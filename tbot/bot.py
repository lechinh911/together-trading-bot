"""Asyncio service: universe -> history warm-up (stored candles + rate-limited REST) -> realtime kline WebSockets
(closed candles only) -> PairStrategy (engine + virtual trade) per symbol+TF -> SQLite -> bot.json (GitHub `data`
branch) for the dashboard. Signal-only: no orders, no API keys."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import time
from dataclasses import dataclass, field
from typing import Optional

import aiohttp

from .config import HTF_TF, TF_MS, Settings
from .engine import Engine
from .htf import CloseEmaSeries, htf_dir
from .strategy import PairStrategy
from .publisher import Publisher
from .ratelimit import WeightLimiter
from .rest import Rest
from .store import Store
from .trades import Trade
from .universe import filter_universe

log = logging.getLogger("tbot")


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class KeyState:
    sym: str
    tf: str
    strat: PairStrategy
    last_t: Optional[int] = None
    warm: bool = False
    busy: bool = True  # backfilling: live bars are buffered
    buffer: list = field(default_factory=list)
    db_trade: Optional[Trade] = None  # open public trade restored from SQLite, attached when replay reaches it

    @property
    def engine(self) -> Engine:
        return self.strat.engine


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
        self.force = False  # set by force_reconnect: skip the back-off


class Bot:
    def __init__(self, settings: Settings | None = None):
        self.S = settings or Settings()
        self.P = self.S.params
        os.makedirs(self.S.data_dir, exist_ok=True)
        self.store = Store(os.path.join(self.S.data_dir, "bot.sqlite"))
        self.lim = WeightLimiter(self.S.weight_budget_per_min, state_file=os.path.join(self.S.data_dir, "rest_ban.json"))
        self.keys: dict = {}
        self.htf_hist: dict = {}  # sym -> CloseEmaSeries of the 4h closes (HTF bias for every TF)
        self.universe: list = []  # [(sym, qvol)]
        self.qvol: dict = {}
        self.symbols: set = set()
        self.conns: list = []
        self.last_price: dict = {}
        self.open: dict = {}  # (sym, tf) -> Trade
        self.recent_closed: list = []
        self.signals_log: list = self.store.kv_get("signals_log", [])
        self.started = time.time()
        self.counters = dict(ws_msgs=0, closed_bars=0, live_bars=0, gaps=0, backfills=0, signals=0, signals_skipped=0,
                             ws_silence=0, resumes=0, catchups=0, fee_dominated=0)
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
            self.htf_hist.pop(s, None)
        for s in sorted(added):
            hist = self.htf_hist[s] = CloseEmaSeries(self.P.htf_ema)
            for tf in self.S.timeframes:
                ks = self.keys[(s, tf)] = KeyState(s, tf, PairStrategy(self.P, s, tf, hist if tf == HTF_TF else None))
                ks.db_trade = self.open.get((s, tf))
        if not first:
            self._sync_streams()
            await self._warm_symbols(sorted(added))

    # ---------------- history / backfill ----------------
    async def _warm_symbols(self, syms):
        """Replay stored candles, then REST-backfill. The 4h HTF first so every other TF sees its HTF bias."""
        conc = max(1, self.S.backfill_concurrency)  # small parallelism; the weight limiter still caps REST/min

        async def pool(items, fn):
            it = iter(items)

            async def worker():
                for x in it:
                    await fn(x)
                    await asyncio.sleep(0)
            await asyncio.gather(*[worker() for _ in range(conc)])

        order = sorted(self.S.timeframes, key=lambda x: (x != HTF_TF, -TF_MS[x]))  # 4h, 1d, 1h, 15m, 5m
        for tf in order:
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
                self._process(ks, b, live=True)  # only the last ~2 bars can publish (recency rule in _process)
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

    def _process(self, ks: KeyState, bar, live: bool, store: bool = True):
        """One CLOSED bar through the Pine-ordered strategy. A signal opens a PUBLIC virtual trade only if the bar came
        from the live feed or a REST fill (live=True), closed within the last 2 bars, and no restored open trade of
        this pair is still waiting to be re-attached. Otherwise (stored-candle replay, older history) the same logic
        runs with a shadow trade so setup / position / cooldown state matches the Pine strategy, unpublished."""
        if ks.last_t is not None and bar[0] <= ks.last_t:
            return
        key = (ks.sym, ks.tf)
        st = ks.strat
        if ks.db_trade is not None and bar[0] > ks.db_trade.entry_t:
            st.trade, st.trade_public = ks.db_trade, True  # restored open trade takes over from the shadow state
            ks.db_trade = None
        tf_ms = TF_MS[ks.tf]
        hd = htf_dir(self.htf_hist.get(ks.sym), TF_MS[HTF_TF], bar[0], tf_ms, bar[4]) if self.P.use_htf else 0
        was_public = st.trade_public
        publish = (live and ks.db_trade is None and bar[0] + 2 * tf_ms >= now_ms()
                   and not any(r["t"] == bar[0] and r["symbol"] == ks.sym and r["tf"] == ks.tf for r in self.signals_log[:50]))
        sig, opened, closed = st.on_bar(bar, hd, publish=publish)
        ks.last_t = bar[0]
        if store:
            self.store.add_candle(ks.sym, ks.tf, bar)
        self.counters["closed_bars"] += 1
        if closed is not None and was_public:
            self.open.pop(key, None)
            self.store.save_trade(closed)
            self.recent_closed.insert(0, closed)
            self.dirty_event = True
            log.info("CLOSE %s %s %s %s R=%.2f", closed.symbol, closed.tf, closed.side, closed.exit_reason, closed.r)
        elif st.trade is not None and st.trade_public and live and opened is None:
            self.store.save_trade(st.trade)
        if sig is not None:
            if publish and opened is not None and not st.trade_public:
                self.counters["fee_dominated"] += 1
                log.info("skip fee-dominated %s %s %s risk=%.4f%%", sig["symbol"], sig["tf"], sig["side"], opened.risk / opened.entry * 100)
            elif publish and opened is not None:
                self.store.save_trade(opened)
                self.open[key] = opened
                rec = dict(symbol=sig["symbol"], tf=sig["tf"], side=sig["side"], t=sig["t"], entry=sig["entry"],
                           quality=sig.get("quality"), adx=sig.get("adx"), score=sig.get("score"), neck=sig.get("neck"),
                           pattern_sl=sig.get("pattern_sl"), detected_at=now_ms(), trade_id=opened.id,
                           stop=opened.stop, tp=opened.tp)
                self.signals_log.insert(0, rec)
                del self.signals_log[300:]
                self.store.kv_set("signals_log", self.signals_log)
                self.counters["signals"] += 1
                self.dirty_event = True
                log.info("SIGNAL %s %s %s entry=%s q=%s", sig["symbol"], sig["tf"], sig["side"], sig["entry"], sig.get("quality"))
            else:
                self.counters["signals_skipped"] += 1

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
                # heartbeat: client ping every 20 s, the socket is closed if no pong arrives (dead TCP after a network
                # drop / VM pause is noticed even before the silence timeout below). Borrowed from opencode stream.py.
                async with self.session.ws_connect(url, heartbeat=self.S.ws_ping_s, autoping=True, max_msg_size=0,
                                                   timeout=aiohttp.ClientWSTimeout(ws_close=3)) as ws:
                    c.ws, c.connected, c.connected_at = ws, True, time.time()
                    log.info("ws#%d connected (%d streams)%s", c.cid, len(c.streams), "" if first else " [reconnect]")
                    if not first:
                        self._after_reconnect(c)
                    first = False
                    backoff = 1.0
                    last_data = time.time()
                    while True:
                        try:
                            # kline streams tick several times per second: no DATA for ws_silence_s = dead / stuck
                            # stream. Hard deadline from the last data frame (wait_for, as in opencode): pongs are
                            # consumed inside receive() and would restart its own timeout forever.
                            left = last_data + self.S.ws_silence_s - time.time()
                            if left <= 0:
                                raise asyncio.TimeoutError
                            msg = await asyncio.wait_for(ws.receive(), left)
                        except asyncio.TimeoutError:
                            self.counters["ws_silence"] += 1
                            log.warning("ws#%d silent for %ds -> reconnect", c.cid, self.S.ws_silence_s)
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            last_data = time.time()
                            c.msgs += 1
                            c.last_msg = time.time()
                            self.counters["ws_msgs"] += 1
                            self._on_ws(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING):
                            log.warning("ws#%d closed by peer (%s)", c.cid, msg.type.name)
                            break
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("ws#%d error: %r", c.cid, e)
            c.connected, c.ws = False, None
            c.reconnects += 1
            if c.force:  # deliberate reconnect (resume / ops signal): reconnect right away
                c.force, backoff = False, 1.0
                await asyncio.sleep(random.random())
                continue
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 60)

    def force_reconnect(self, reason: str, cids=None):
        """Close the given (default: all) sockets; conn_loop reconnects within ~1 s and _after_reconnect gap-fills."""
        n = 0
        for c in self.conns:
            if (cids is None or c.cid in cids) and c.ws is not None and not c.ws.closed:
                c.force = True
                asyncio.get_event_loop().create_task(c.ws.close())
                n += 1
        log.warning("force reconnect of %d ws (%s)", n, reason)

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

    def _resync_silent(self, grace_ms: int = 30_000):
        """Catch-up (idea from opencode stream.py _catchup_once): every pair whose last fully closed bar is missing
        `grace_ms` after it closed gets a REST gap-fill of only the missing bars (start_time = last_t + 1 bar), paced
        by the shared weight limiter. Returns the number of pairs scheduled."""
        nm = now_ms()
        n = 0
        for ks in self.keys.values():
            if not ks.warm or ks.busy or ks.last_t is None:
                continue
            tf_ms = TF_MS[ks.tf]
            expect = (nm // tf_ms) * tf_ms - tf_ms  # open time of the last bar that has closed
            if ks.last_t < expect and nm - (expect + tf_ms) > grace_ms:
                self.schedule_backfill(ks)
                n += 1
        if n:
            self.counters["catchups"] += n
            log.info("catch-up: %d pairs missing closed bars scheduled for gap-fill", n)
        return n

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
        tick = time.time()
        while True:
            await asyncio.sleep(5)
            t = time.time()
            gap, tick = t - tick, t
            try:
                if gap > 20 and self.startup.get("warm_done_at"):
                    # the event loop / box was frozen (VM pause, suspend): sockets are dead, bars are missing
                    self.counters["resumes"] += 1
                    log.warning("resume after %.0fs freeze: reconnect all ws + catch-up", gap)
                    self.force_reconnect("resume")
                    self._resync_silent(grace_ms=0)
                    last_resync = t
                if t - last_flush >= 10:
                    self.store.flush()
                    last_flush = t
                pend = self.publisher.pending_at
                if t - last_pub >= self.S.publish_every_s or self.dirty_event or (pend and t >= pend):
                    await self.publisher.publish()
                    last_pub = t
                if t - last_uni >= self.S.universe_refresh_s:
                    last_uni = t
                    await self.refresh_universe()
                if t - last_resync >= self.S.catchup_every_s and self.startup.get("warm_done_at"):
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
            try:  # ops: `kill -USR1 <pid>` forces a reconnect of every socket (used to test recovery)
                asyncio.get_event_loop().add_signal_handler(signal.SIGUSR1, lambda: self.force_reconnect("SIGUSR1"))
            except (NotImplementedError, RuntimeError):
                pass
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
