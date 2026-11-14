"""Asyncio service: universe -> history backfill (rate-limited REST) -> realtime kline WebSockets -> engine ->
virtual trades -> JSON for the website. Signal-only: no orders, no API keys."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from .config import HTF_MAP, TF_MS, Settings
from .engine import Engine
from .htf import SimpleSeries, htf_dir
from .publisher import Publisher
from .ratelimit import WeightLimiter
from .rest import Rest
from .store import Store
from .trades import Trade, apply_bar, open_trade
from .universe import filter_universe

log = logging.getLogger("tbot")


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class KeyState:
    sym: str
    tf: str
    engine: Engine
    last_t: Optional[int] = None
    warm: bool = False
    busy: bool = True  # backfilling: live bars are buffered
    buffer: list = field(default_factory=list)
    last_exit_idx: Optional[int] = None


class Conn:
    def __init__(self, cid: int, streams: set):
        self.cid, self.streams = cid, set(streams)
        self.connected = False
        self.ws = None
        self.reconnects = 0
        self.msgs = 0
        self.last_msg = 0.0
        self.connected_at = 0.0
        self.task = None
        self.msg_id = 0


class Bot:
    def __init__(self, settings: Settings | None = None):
        self.S = settings or Settings()
        self.P = self.S.params
        os.makedirs(self.S.data_dir, exist_ok=True)
        self.store = Store(os.path.join(self.S.data_dir, "bot.sqlite"))
        self.lim = WeightLimiter(self.S.weight_budget_per_min, state_file=os.path.join(self.S.data_dir, "rest_ban.json"))
        self.keys: dict = {}
        self.weekly: dict = {}
        self.universe: list = []  # [(sym, qvol)]
        self.qvol: dict = {}
        self.symbols: set = set()
        self.conns: list = []
        self.last_price: dict = {}
        self.open: dict = {}  # (sym, tf) -> Trade
        self.recent_closed: list = []
        self.signals_log: list = self.store.kv_get("signals_log", [])
        self.started = time.time()
        self.counters = dict(ws_msgs=0, closed_bars=0, live_bars=0, gaps=0, backfills=0, signals=0, signals_skipped=0)
        self.startup = dict(rest_calls=None, rest_weight=None, warm_done_at=None)
        self.universe_at = 0.0
        self.dirty_event = False
        self.bf_queue: asyncio.Queue = None
        self.bf_inflight: set = set()
        self.publisher = Publisher(self)
        for tr in self.store.load_trades("open"):
            self.open[(tr.symbol, tr.tf)] = tr

    # ---------------- universe ----------------
    async def refresh_universe(self, first=False):
        ei = await self.rest.exchange_info()
        tk = await self.rest.tickers_24h()
        if not ei or not tk:
            saved = self.store.kv_get("universe") if first else None
            if saved:
                log.warning("universe REST failed; using last saved universe (%d symbols)", len(saved))
                ei = {"symbols": [{"symbol": x, "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"} for x, _ in saved]}
                tk = [{"symbol": x, "quoteVolume": q} for x, q in saved]
            else:
                log.warning("universe refresh failed; keeping %d symbols", len(self.symbols))
                return
        uni = filter_universe(ei, tk, self.S.min_quote_volume)
        self.store.kv_set("universe", uni)
        self.universe = uni
        self.qvol = {s: q for s, q in uni}
        new = {s for s, _ in uni} | {k[0] for k in self.open}  # keep streaming symbols with open virtual trades
        tradable = {s["symbol"] for s in ei.get("symbols", []) if s.get("status") == "TRADING"}
        new = {s for s in new if s in tradable}
        added, removed = new - self.symbols, self.symbols - new
        self.symbols = new
        self.universe_at = time.time()
        log.info("universe: %d symbols (>= %.0f USDT 24h), +%d -%d", len(new), self.S.min_quote_volume, len(added), len(removed))
        for s in removed:
            for tf in self.S.timeframes:
                self.keys.pop((s, tf), None)
            self.weekly.pop(s, None)
        for s in sorted(added):
            for tf in self.S.timeframes:
                self.keys[(s, tf)] = KeyState(s, tf, Engine(self.P, s, tf))
        if not first:
            self._sync_streams()
            await self._warm_symbols(sorted(added))

    # ---------------- history / backfill ----------------
    async def _fetch_weekly(self, sym):
        bars = await self.rest.klines(sym, "1w", limit=99)
        ser = SimpleSeries(self.P.htf_ema)
        nm = now_ms()
        for b in bars:
            if b[6] < nm:
                ser.add(b[0], b[4])
        self.weekly[sym] = ser

    async def _warm_symbols(self, syms):
        """Replay stored candles, then REST-backfill. HTF first so lower TFs see their HTF bias."""
        conc = max(1, self.S.backfill_concurrency)  # small parallelism; the weight limiter still caps REST/min

        async def pool(items, fn):
            it = iter(items)

            async def worker():
                for x in it:
                    await fn(x)
                    await asyncio.sleep(0)
            await asyncio.gather(*[worker() for _ in range(conc)])

        await pool([s for s in syms if s not in self.weekly], self._fetch_weekly)
        for tf in sorted(self.S.timeframes, key=lambda x: -TF_MS[x]):  # HTF first
            await pool([self.keys[(s, tf)] for s in syms if (s, tf) in self.keys], self._warm_key)

    async def _warm_key(self, ks: KeyState):
        tf_ms = TF_MS[ks.tf]
        nm = now_ms()
        expect_last = (nm // tf_ms) * tf_ms - tf_ms  # open time of the last fully closed bar
        stored = self.store.load_candles(ks.sym, ks.tf, self.S.keep_bars)
        if stored and (expect_last - stored[-1][0]) // tf_ms > 400:
            stored = []  # too old: fetch fresh history
        for b in stored:
            self._process(ks, b, live=False, store=False)
        try:
            await self._backfill(ks, fresh=not stored)
        finally:
            ks.warm = True
            self._drain(ks)

    async def _backfill(self, ks: KeyState, fresh=False):
        tf_ms = TF_MS[ks.tf]
        for _ in range(5):
            nm = now_ms()
            expect_last = (nm // tf_ms) * tf_ms - tf_ms
            if ks.last_t is not None and ks.last_t >= expect_last:
                return
            if fresh or ks.last_t is None:
                bars = await self.rest.klines(ks.sym, ks.tf, limit=self.S.history_limit)
            else:
                need = (expect_last - ks.last_t) // tf_ms + 2
                lim = 99 if need <= 99 else self.S.history_limit
                bars = await self.rest.klines(ks.sym, ks.tf, limit=lim, start_time=ks.last_t + tf_ms)
            self.counters["backfills"] += 1
            nm = now_ms()
            closed = [b[:6] for b in bars if b[6] < nm]
            if not closed:
                return
            for b in closed:
                self._process(ks, b, live=ks.warm)
            fresh = False
            if len(bars) < 99:
                return

    def _drain(self, ks: KeyState):
        ks.busy = False
        buf = sorted(ks.buffer)
        ks.buffer = []
        for b in buf:
            self.on_closed_bar(ks, b)

    async def backfill_worker(self):
        while True:
            key = await self.bf_queue.get()
            ks = self.keys.get(key)
            try:
                if ks is not None:
                    await self._backfill(ks)
            except Exception:
                log.exception("backfill %s", key)
            finally:
                self.bf_inflight.discard(key)
                if ks is not None:
                    self._drain(ks)

    def schedule_backfill(self, ks: KeyState):
        key = (ks.sym, ks.tf)
        ks.busy = True
        if key not in self.bf_inflight:
            self.bf_inflight.add(key)
            self.counters["gaps"] += 1
            self.bf_queue.put_nowait(key)

    # ---------------- bar processing ----------------
    def on_closed_bar(self, ks: KeyState, bar):
        if ks.busy:
            ks.buffer.append(bar)
            return
        tf_ms = TF_MS[ks.tf]
        if ks.last_t is not None and bar[0] > ks.last_t + tf_ms:
            ks.buffer.append(bar)
            self.schedule_backfill(ks)
            return
        self._process(ks, bar, live=True)

    def _htf_series(self, sym, tf):
        h = HTF_MAP[tf]
        if h == "1w":
            return self.weekly.get(sym), TF_MS["1w"]
        k = self.keys.get((sym, h))
        return (k.engine if k else None), TF_MS[h]

    def _process(self, ks: KeyState, bar, live: bool, store: bool = True):
        if ks.last_t is not None and bar[0] <= ks.last_t:
            return
        eng = ks.engine
        key = (ks.sym, ks.tf)
        tr = self.open.get(key)
        if tr is not None and bar[0] > (tr.last_t or 0):
            atr_now = eng.peek_atr(bar)
            if apply_bar(tr, bar, atr_now, self.P):
                self.open.pop(key, None)
                ks.last_exit_idx = eng.n
                self.store.save_trade(tr)
                self.recent_closed.insert(0, tr)
                self.dirty_event = True
                log.info("CLOSE %s %s %s %s R=%.2f", tr.symbol, tr.tf, tr.side, tr.exit_reason, tr.r)
            else:
                self.store.save_trade(tr) if live else None
        cool_ok = ks.last_exit_idx is None or eng.n - ks.last_exit_idx > self.P.cooldown
        flat = key not in self.open
        series, hms = self._htf_series(ks.sym, ks.tf)
        hd = htf_dir(series, hms, bar[0]) if self.P.use_htf else 0
        sig = eng.process_bar(bar, hd, cool_ok, flat)
        ks.last_t = bar[0]
        if store:
            self.store.add_candle(ks.sym, ks.tf, bar)
        self.counters["closed_bars"] += 1
        if sig:
            recent = bar[0] + 2 * TF_MS[ks.tf] >= now_ms()
            if live and ks.warm and recent:
                trd = open_trade(sig, self.P)
                rec = dict(symbol=sig["symbol"], tf=sig["tf"], side=sig["side"], t=sig["t"], entry=sig["entry"],
                           quality=sig.get("quality"), adx=sig.get("adx"), score=sig.get("score"), neck=sig.get("neck"),
                           pattern_sl=sig.get("pattern_sl"), detected_at=now_ms())
                if trd:
                    self.store.save_trade(trd)
                    self.open[key] = trd
                    rec.update(trade_id=trd.id, stop=trd.stop, tp=trd.tp)
                else:
                    rec.update(skipped="risk<=0")
                self.signals_log.insert(0, rec)
                del self.signals_log[300:]
                self.store.kv_set("signals_log", self.signals_log)
                self.counters["signals"] += 1
                self.dirty_event = True
                log.info("SIGNAL %s %s %s entry=%s q=%s", sig["symbol"], sig["tf"], sig["side"], sig["entry"], sig.get("quality"))
            else:
                self.counters["signals_skipped"] += 1
        if ks.tf == "1d" and live:
            self._maybe_refresh_weekly(ks.sym)

    def _maybe_refresh_weekly(self, sym):
        ser = self.weekly.get(sym)
        wk = TF_MS["1w"]
        # Binance weeks open Monday 00:00 UTC; epoch day 0 is Thursday -> offset 4 days
        off = 4 * 86_400_000
        cur_open = ((now_ms() - off) // wk) * wk + off
        if ser is None or not ser.t or ser.t[-1] < cur_open - wk:
            asyncio.get_event_loop().create_task(self._fetch_weekly(sym))

    # ---------------- websockets ----------------
    def all_streams(self):
        return {f"{s.lower()}@kline_{tf}" for s in self.symbols for tf in self.S.timeframes}

    def _sync_streams(self):
        want = self.all_streams()
        have = set().union(*[c.streams for c in self.conns]) if self.conns else set()
        for c in self.conns:
            rm = c.streams - want
            if rm:
                c.streams -= rm
                self._send(c, "UNSUBSCRIBE", sorted(rm))
        add = sorted(want - have)
        for c in self.conns:
            room = self.S.streams_per_conn - len(c.streams)
            if room > 0 and add:
                chunk, add = add[:room], add[room:]
                c.streams |= set(chunk)
                self._send(c, "SUBSCRIBE", chunk)
        while add:
            chunk, add = add[: self.S.streams_per_conn], add[self.S.streams_per_conn:]
            c = Conn(len(self.conns), set(chunk))
            self.conns.append(c)
            c.task = asyncio.get_event_loop().create_task(self.conn_loop(c, delay=0.5 * len(self.conns)))
        self.conns = [c for c in self.conns if c.streams or not c.task.cancel()]

    def _send(self, c: Conn, method, params):
        if c.ws is None or c.ws.closed:
            return  # applied on reconnect via URL
        for i in range(0, len(params), 50):  # Binance silently drops large SUBSCRIBE messages
            c.msg_id += 1
            asyncio.get_event_loop().create_task(c.ws.send_str(json.dumps({"method": method, "params": params[i:i + 50], "id": c.msg_id})))

    async def conn_loop(self, c: Conn, delay=0.0):
        await asyncio.sleep(delay)
        backoff = 1.0
        first = True
        while True:
            if not c.streams:
                return
            url = self.S.ws_base + "?streams=" + "/".join(sorted(c.streams))
            try:
                async with self.session.ws_connect(url, heartbeat=None, autoping=True, max_msg_size=0,
                                                   timeout=aiohttp.ClientWSTimeout(ws_close=10)) as ws:
                    c.ws, c.connected, c.connected_at = ws, True, time.time()
                    log.info("ws#%d connected (%d streams)%s", c.cid, len(c.streams), "" if first else " [reconnect]")
                    if not first:
                        self._after_reconnect(c)
                    first = False
                    backoff = 1.0
                    while True:
                        msg = await ws.receive(timeout=90)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            c.msgs += 1
                            c.last_msg = time.time()
                            self.counters["ws_msgs"] += 1
                            self._on_ws(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING):
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ws#%d error: %r", c.cid, e)
            c.connected, c.ws = False, None
            c.reconnects += 1
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)

    def _after_reconnect(self, c: Conn):
        nm = now_ms()
        for st in c.streams:
            s, _, tf = st.partition("@kline_")
            ks = self.keys.get((s.upper(), tf))
            if ks is None or not ks.warm or ks.busy:
                continue
            tf_ms = TF_MS[tf]
            if ks.last_t is not None and ks.last_t < (nm // tf_ms) * tf_ms - tf_ms:
                self.schedule_backfill(ks)

    def _resync_silent(self):
        """Pairs whose last closed bar is >1 bar behind (silent stream, dropped close) get a small REST gap-fill."""
        nm = now_ms()
        n = 0
        for ks in self.keys.values():
            if not ks.warm or ks.busy or ks.last_t is None:
                continue
            tf_ms = TF_MS[ks.tf]
            expect = (nm // tf_ms) * tf_ms - tf_ms
            if ks.last_t < expect - tf_ms and nm - expect > 30_000:
                self.schedule_backfill(ks)
                n += 1
        if n:
            log.info("resync: %d silent pairs scheduled for gap-fill", n)

    def _on_ws(self, raw):
        try:
            d = json.loads(raw)
        except ValueError:
            return
        data = d.get("data") if isinstance(d, dict) else None
        if not data or data.get("e") != "kline":
            return
        k = data["k"]
        sym = data["s"]
        try:
            self.last_price[sym] = float(k["c"])
        except (KeyError, ValueError):
            pass
        if not k.get("x"):
            return
        ks = self.keys.get((sym, k["i"]))
        if ks is None:
            return
        bar = (int(k["t"]), float(k["o"]), float(k["h"]), float(k["l"]), float(k["c"]), float(k["v"]))
        self.counters["live_bars"] += 1
        self.on_closed_bar(ks, bar)

    # ---------------- periodic ----------------
    async def periodic(self):
        last_flush = last_pub = last_uni = last_prune = last_resync = time.time()
        while True:
            await asyncio.sleep(5)
            t = time.time()
            try:
                if t - last_flush >= 10:
                    self.store.flush()
                    last_flush = t
                if t - last_pub >= self.S.publish_every_s or self.dirty_event:
                    await self.publisher.publish()
                    last_pub = t
                if t - last_uni >= self.S.universe_refresh_s:
                    last_uni = t
                    await self.refresh_universe()
                if t - last_resync >= 600 and self.startup.get("warm_done_at"):
                    last_resync = t
                    self._resync_silent()
                if t - last_prune >= 3600:
                    last_prune = t
                    self.store.prune(self.S.keep_bars)
            except Exception:
                log.exception("periodic")

    async def run(self):
        self.bf_queue = asyncio.Queue()
        async with aiohttp.ClientSession(headers={"User-Agent": "together-trading-bot/1.0"}) as session:
            self.session = session
            self.rest = Rest(self.S.rest_base, self.lim, session)
            await self.refresh_universe(first=True)
            while not self.symbols:
                log.warning("empty universe, retry in 60s")
                await asyncio.sleep(60)
                await self.refresh_universe(first=True)
            self._sync_streams()  # connect first: live closes are buffered during warmup
            pub = asyncio.get_event_loop().create_task(self.periodic())
            for _ in range(2):
                asyncio.get_event_loop().create_task(self.backfill_worker())
            t0 = time.time()
            await self._warm_symbols(sorted(self.symbols))
            self.store.flush()
            st = self.lim.stats()
            self.startup = dict(rest_calls=st["calls"], rest_weight=st["weight_total"], weight_peak_60s=st["weight_peak_60s"],
                                server_weight_peak=st["server_weight_peak"], warm_seconds=round(time.time() - t0, 1),
                                warm_done_at=time.time())
            log.info("warmup done: %s", self.startup)
            self.dirty_event = True
            await pub
