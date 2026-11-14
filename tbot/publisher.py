"""Writes the website JSON (data/bot.json) and requests coalesced Vercel deploys through the existing deploy script."""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import time

from .config import TF_MS
from .trades import open_r

log = logging.getLogger("tbot.pub")
TZ7 = dt.timezone(dt.timedelta(hours=7))


def t7(ms_or_s, ms=True, fmt="%Y-%m-%d %H:%M"):
    if ms_or_s is None:
        return None
    ts = ms_or_s / 1000 if ms else ms_or_s
    return dt.datetime.fromtimestamp(ts, TZ7).strftime(fmt)


def rnd(x, n=6):
    try:
        if x is None or x != x:
            return None
        return float(f"{x:.{n}g}")
    except (TypeError, ValueError):
        return None


def stats_for(trades):
    n = len(trades)
    if not n:
        return dict(n=0, wins=0, losses=0, win_rate=None, avg_r=None, sum_r=None, avg_pct=None, best_r=None, worst_r=None,
                    by_reason={})
    rs = [t.r for t in trades]
    wins = sum(1 for r in rs if r > 0)
    reasons = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    return dict(n=n, wins=wins, losses=n - wins, win_rate=round(wins / n * 100, 1), avg_r=round(sum(rs) / n, 3),
                sum_r=round(sum(rs), 2), avg_pct=round(sum(t.pct for t in trades) / n, 3),
                best_r=round(max(rs), 2), worst_r=round(min(rs), 2), by_reason=reasons)


class Publisher:
    def __init__(self, bot):
        self.bot = bot
        self.last_push = 0.0
        self.last_sig = None
        self.pushing = False
        self.pusher = None
        if bot.S.push_enabled:
            from .gitpush import DataPusher
            self.pusher = DataPusher(bot.S.repo_url, os.path.join(bot.S.data_dir, "gitdata"), bot.S.data_branch)

    def trade_row(self, tr, live_price=None):
        S = self.bot.P
        d = dict(id=tr.id, symbol=tr.symbol, tf=tr.tf, side=tr.side, entry_t=tr.entry_t, entry_t7=t7(tr.entry_t + TF_MS[tr.tf]),
                 entry=rnd(tr.entry), stop=rnd(tr.stop), stop0=rnd(tr.stop0), tp=rnd(tr.tp), risk_pct=rnd(tr.risk / tr.entry * 100, 4),
                 quality=tr.quality, be=tr.be, trail=tr.trail is not None, mfe_r=rnd(tr.mfe_r, 3), neck=rnd(tr.neck))
        if tr.status == "open":
            px = live_price or tr.last_close
            d.update(last=rnd(px), open_R=rnd(open_r(tr, px, S), 3) if px else None)
        else:
            d.update(exit_t=tr.exit_t, exit_t7=t7(tr.exit_t + TF_MS[tr.tf]) if tr.exit_t else None, exit=rnd(tr.exit_price),
                     reason=tr.exit_reason, r=rnd(tr.r, 3), pct=rnd(tr.pct, 3),
                     bars=int((tr.exit_t - tr.entry_t) // TF_MS[tr.tf]) if tr.exit_t else None)
        return d

    def build(self):
        b = self.bot
        closed = b.store.load_trades("closed", limit=2000)
        open_trades = sorted(b.open.values(), key=lambda t: -t.entry_t)
        tfs = b.S.timeframes
        stats = {"ALL": stats_for(closed)}
        for tf in tfs:
            stats[tf] = stats_for([t for t in closed if t.tf == tf])
            stats[tf]["open"] = sum(1 for t in open_trades if t.tf == tf)
            stats[tf]["signals"] = sum(1 for s in b.signals_log if s["tf"] == tf)
        stats["ALL"]["open"] = len(open_trades)
        stats["ALL"]["signals"] = len(b.signals_log)
        for side in ("LONG", "SHORT"):
            stats["side_" + side] = stats_for([t for t in closed if t.side == side])
        armed = []
        n_armed = {tf: 0 for tf in tfs}
        n_broken = {tf: 0 for tf in tfs}
        for (sym, tf), ks in b.keys.items():
            for a in ks.engine.armed_state():
                n_armed[tf] += 1
                n_broken[tf] += int(a["broken"])
                if a["broken"]:
                    armed.append(dict(symbol=sym, tf=tf, side=a["side"], neck=rnd(a["neck"]), sl=rnd(a["sl"]),
                                      quality=a["quality"], break_t7=t7(a["break_t"] + TF_MS[tf]) if a["break_t"] else None,
                                      break_t=a["break_t"]))
        armed.sort(key=lambda x: -(x["break_t"] or 0))
        lim = b.lim.stats()
        conns = [dict(id=c.cid, streams=len(c.streams), connected=c.connected, reconnects=c.reconnects, msgs=c.msgs,
                      last_msg_age_s=round(time.time() - c.last_msg, 1) if c.last_msg else None) for c in b.conns]
        warm = sum(1 for k in b.keys.values() if k.warm)
        signals = []
        for s in b.signals_log[:150]:
            r = dict(s)
            r["t7"] = t7(s["t"] + TF_MS[s["tf"]])
            for k in ("entry", "neck", "pattern_sl", "stop", "tp"):
                if k in r:
                    r[k] = rnd(r[k])
            r["adx"] = rnd(r.get("adx"), 3)
            tr = b.open.get((s["symbol"], s["tf"]))
            r["state"] = "open" if tr is not None and tr.id == s.get("trade_id") else ("skipped" if s.get("skipped") else "closed")
            signals.append(r)
        now = time.time()
        status = dict(
            started_t7=t7(b.started, ms=False), uptime_s=int(now - b.started), universe=len(b.symbols),
            universe_min_qvol=b.S.min_quote_volume, universe_t7=t7(b.universe_at, ms=False) if b.universe_at else None,
            timeframes=tfs, streams=sum(len(c.streams) for c in b.conns), connections=len(b.conns),
            connected=sum(1 for c in b.conns if c.connected), conns=conns, keys=len(b.keys), keys_warm=warm,
            counters=dict(b.counters), rest=lim, startup=b.startup, preset=b.S.preset,
            setups_waiting=n_armed, setups_broken=n_broken,
            params=dict(vars(b.P)) if hasattr(b.P, "__dict__") else {},
        )
        top = sorted(b.universe, key=lambda x: -x[1])[:10]
        return dict(schema="tbot-1", generated=now, generated_t7=t7(now, ms=False, fmt="%Y-%m-%d %H:%M:%S"),
                    status=status, signals=signals, open=[self.trade_row(t, b.last_price.get(t.symbol)) for t in open_trades],
                    closed=[self.trade_row(t) for t in closed[:300]], stats=stats, waiting=armed[:120],
                    top_volume=[dict(symbol=s, qvol=round(q)) for s, q in top])

    def write(self, data) -> bytes:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        for path in (os.path.join(self.bot.S.data_dir, "bot.json"),):
            with open(path + ".tmp", "wb") as f:
                f.write(raw)
            os.replace(path + ".tmp", path)
        st = os.path.join(self.bot.S.data_dir, "status.json")
        with open(st + ".tmp", "w") as f:
            json.dump(data["status"], f, ensure_ascii=False, indent=1)
        os.replace(st + ".tmp", st)
        return raw

    @staticmethod
    def content_sig(data) -> str:
        """Hash of the trading content (signals, trades, stats, waiting setups) - excludes status/timestamps."""
        import hashlib
        core = {k: data.get(k) for k in ("signals", "closed", "stats", "waiting")}
        core["open"] = [{k: v for k, v in r.items() if k not in ("last", "open_R")} for r in data.get("open", [])]
        return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()[:16]

    async def publish(self):
        b = self.bot
        b.dirty_event = False
        data = self.build()
        data["status"]["github"] = dict(pushes=self.pusher.pushes if self.pusher else 0,
                                        last_push_t7=t7(self.pusher.last_push_t, ms=False) if self.pusher and self.pusher.last_push_t else None,
                                        last_error=self.pusher.last_error if self.pusher else None)
        raw = self.write(data)
        if not self.pusher or b.startup.get("warm_done_at") is None or self.pushing:
            return  # never publish a half-warm dataset
        t = time.time()
        sig = self.content_sig(data)
        changed = sig != self.last_sig
        if (changed and t - self.last_push >= b.S.push_every_s) or t - self.last_push >= b.S.heartbeat_push_s:
            self.pushing = True
            try:
                n_sig, n_open = len(data["signals"]), len(data["open"])
                msg = f"data {data['generated_t7']} UTC+7: {n_sig} tín hiệu, {n_open} lệnh mở, {len(data['closed'])} lệnh đóng"
                ok = await self.pusher.push({"bot.json": raw}, msg)
                self.last_push = t
                if ok:
                    self.last_sig = sig
                    self.pusher.last_push_t = time.time()
                    log.info("pushed data branch (%s)", "content changed" if changed else "heartbeat")
            finally:
                self.pushing = False
