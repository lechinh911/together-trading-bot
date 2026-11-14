"""Virtual trade model (Active exits). No real orders are ever placed.

Entry at the signal bar close. Stop = pattern extreme capped at 3 ATR; TP = 1.5R. Each later bar:
  1. exits are checked with the stop/TP that were active at the start of the bar; if both are touched in the same
     bar the STOP is assumed first (conservative); gaps fill at the open when worse/better than the level;
  2. if the bar reached +1R, stop -> breakeven and the ATR trail starts: trail = max(prevTrail, close - 1.30*ATR)
     (short mirrored), stop = max(stop, trail). TP stays at 1.5R.
PnL includes 0.04% commission per side.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from .config import Params
from .indicators import finite


@dataclass
class Trade:
    symbol: str
    tf: str
    side: str
    entry_t: int
    entry: float
    stop0: float
    stop: float
    tp: float
    risk: float
    atr: float
    quality: Optional[float] = None
    neck: Optional[float] = None
    pattern_sl: Optional[float] = None
    be: bool = False
    trail: Optional[float] = None
    status: str = "open"
    exit_t: Optional[int] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    r: Optional[float] = None
    pct: Optional[float] = None
    last_t: Optional[int] = None  # open time of the last bar applied
    last_close: Optional[float] = None
    mfe_r: float = 0.0
    id: Optional[int] = None
    pattern: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def open_trade(sig: dict, p: Params) -> Optional[Trade]:
    entry, a, raw = sig["entry"], sig["atr"], sig["pattern_sl"]
    if not (finite(entry) and finite(a) and finite(raw)):
        return None
    if sig["side"] == "LONG":
        stop = max(raw, entry - p.stop_cap_atr * a)
        risk = entry - stop
        tp = entry + p.tp_r * risk
    else:
        stop = min(raw, entry + p.stop_cap_atr * a)
        risk = stop - entry
        tp = entry - p.tp_r * risk
    if not risk > 0:
        return None
    return Trade(symbol=sig["symbol"], tf=sig["tf"], side=sig["side"], entry_t=sig["t"], entry=entry, stop0=stop,
                 stop=stop, tp=tp, risk=risk, atr=a, quality=sig.get("quality"), neck=sig.get("neck"),
                 pattern_sl=raw, last_t=sig["t"], last_close=entry, pattern=sig.get("pattern") or {})


def pnl(tr: Trade, exit_price: float, p: Params):
    gross = (exit_price - tr.entry) if tr.side == "LONG" else (tr.entry - exit_price)
    comm = p.commission * (tr.entry + exit_price)
    r = (gross - comm) / tr.risk
    pct = (gross - comm) / tr.entry * 100.0
    return r, pct


def apply_bar(tr: Trade, bar, atr_now: float, p: Params) -> bool:
    """Apply one CLOSED bar after entry. Returns True if the trade closed on this bar."""
    t, o, h, l, c, _v = bar
    if tr.status != "open" or (tr.last_t is not None and t <= tr.last_t):
        return False
    long = tr.side == "LONG"
    exit_px = reason = None
    if long:
        hit_stop, hit_tp = l <= tr.stop, h >= tr.tp
        if hit_stop:  # stop first when both touched (conservative)
            exit_px = min(o, tr.stop)
            reason = "trail" if tr.trail is not None and tr.stop > tr.entry else ("be" if tr.be and tr.stop >= tr.entry else "sl")
        elif hit_tp:
            exit_px, reason = max(o, tr.tp), "tp"
        tr.mfe_r = max(tr.mfe_r, (h - tr.entry) / tr.risk)
    else:
        hit_stop, hit_tp = h >= tr.stop, l <= tr.tp
        if hit_stop:
            exit_px = max(o, tr.stop)
            reason = "trail" if tr.trail is not None and tr.stop < tr.entry else ("be" if tr.be and tr.stop <= tr.entry else "sl")
        elif hit_tp:
            exit_px, reason = min(o, tr.tp), "tp"
        tr.mfe_r = max(tr.mfe_r, (tr.entry - l) / tr.risk)
    tr.last_t, tr.last_close = t, c
    if exit_px is not None:
        tr.status, tr.exit_t, tr.exit_price, tr.exit_reason = "closed", t, exit_px, reason
        tr.r, tr.pct = pnl(tr, exit_px, p)
        return True
    # management for the next bar
    if long:
        if not tr.be and h >= tr.entry + p.be_at_r * tr.risk:
            tr.be = True
            tr.stop = max(tr.stop, tr.entry)
        if tr.be and finite(atr_now):
            cand = c - p.trail_atr * atr_now
            tr.trail = cand if tr.trail is None else max(tr.trail, cand)
            tr.stop = max(tr.stop, tr.trail)
    else:
        if not tr.be and l <= tr.entry - p.be_at_r * tr.risk:
            tr.be = True
            tr.stop = min(tr.stop, tr.entry)
        if tr.be and finite(atr_now):
            cand = c + p.trail_atr * atr_now
            tr.trail = cand if tr.trail is None else min(tr.trail, cand)
            tr.stop = min(tr.stop, tr.trail)
    return False


def open_r(tr: Trade, price: float, p: Params) -> float:
    return pnl(tr, price, p)[0]
