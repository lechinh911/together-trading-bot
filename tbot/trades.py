"""Virtual trade model (Pine strategy.entry/strategy.exit, Active preset). No real orders are ever placed.

Entry: market at the signal bar CLOSE (process_orders_on_close). Stop = pattern extreme capped at stopCap*ATR
(3 ATR), TP = entry +/- 1.5R. One position per symbol+TF (pyramiding=0).

Each later CLOSED bar, in the Pine order:
  1. `check_exit` (TradingView broker emulator, default bar detail): the stop / TP orders active at the start of
     the bar are checked on it. If the open is already beyond a level the order fills at the open (gap). Otherwise
     TradingView's intrabar path is used: open closer to the high -> open-high-low-close, else open-low-high-close;
     whichever level that path touches first fills at its price.
  2. `manage` (script step, only if still open): if the bar reached +1R (high - entry >= risk), stop -> breakeven
     and the ATR trail switches on; from then on stop = max(stop, close - 1.30*ATR) every bar (short mirrored).
     The new stop applies from the next bar. TP stays at 1.5R.
PnL in R and % includes 0.04% commission per side.
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
    entry_t: int  # open time (ms) of the signal bar; entry price = its close
    entry: float
    stop0: float  # initial stop
    stop: float  # current stop (moves to breakeven / trail)
    tp: float
    risk: float  # entry - stop0 (LONG) / stop0 - entry (SHORT), price units
    atr: float
    quality: Optional[float] = None
    neck: Optional[float] = None
    pattern_sl: Optional[float] = None
    be: bool = False  # +1R reached: breakeven done and trail on (Pine beMoved / trailOn)
    trail: Optional[float] = None  # last ATR-trail candidate level (max for LONG / min for SHORT)
    status: str = "open"
    exit_t: Optional[int] = None  # open time of the exit bar
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # tp / sl / be / trail
    r: Optional[float] = None
    pct: Optional[float] = None
    last_t: Optional[int] = None  # open time of the last bar applied
    last_close: Optional[float] = None
    mfe_r: float = 0.0  # max favourable excursion in R
    id: Optional[int] = None
    pattern: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def open_trade(sig: dict, p: Params) -> Optional[Trade]:
    """Build the virtual position for an engine signal (None if risk <= 0)."""
    entry, a, raw = sig["entry"], sig["atr"], sig["pattern_sl"]
    if not (finite(entry) and finite(a) and finite(raw)):
        return None
    if sig["side"] == "LONG":
        stop = max(raw, entry - p.stop_cap_atr * a) if p.stop_cap_atr > 0 else raw
        risk = entry - stop
        tp = entry + p.tp_r * risk
    else:
        stop = min(raw, entry + p.stop_cap_atr * a) if p.stop_cap_atr > 0 else raw
        risk = stop - entry
        tp = entry - p.tp_r * risk
    if not risk > 0:
        return None
    return Trade(symbol=sig["symbol"], tf=sig["tf"], side=sig["side"], entry_t=sig["t"], entry=entry, stop0=stop,
                 stop=stop, tp=tp, risk=risk, atr=a, quality=sig.get("quality"), neck=sig.get("neck"),
                 pattern_sl=raw, last_t=sig["t"], last_close=entry, pattern=sig.get("pattern") or {})


def fee_dominated(tr: Trade, p: Params) -> bool:
    """Deviation from Pine: risk below min_risk_fees x round-trip commission (2 x 0.08% = 0.16% of entry by default)
    -> the trade is not published (it still runs as a shadow so the Pine position/cooldown state is unchanged)."""
    return p.min_risk_fees > 0 and tr.risk < p.min_risk_fees * 2.0 * p.commission * tr.entry


def pnl(tr: Trade, exit_price: float, p: Params):
    """(R multiple, % of entry) after commission on both sides."""
    gross = (exit_price - tr.entry) if tr.side == "LONG" else (tr.entry - exit_price)
    comm = p.commission * (tr.entry + exit_price)
    return (gross - comm) / tr.risk, (gross - comm) / tr.entry * 100.0


def _stop_reason(tr: Trade) -> str:
    beyond_entry = tr.stop > tr.entry if tr.side == "LONG" else tr.stop < tr.entry
    if tr.trail is not None and beyond_entry:
        return "trail"
    if tr.be and (tr.stop >= tr.entry if tr.side == "LONG" else tr.stop <= tr.entry):
        return "be"
    return "sl"


def check_exit(tr: Trade, bar, p: Params) -> bool:
    """Broker step for one CLOSED bar after the entry bar. Returns True if the trade closed on this bar.
    Bars at or before tr.last_t are ignored (never applied twice)."""
    t, o, h, l, c, _v = bar
    if tr.status != "open" or (tr.last_t is not None and t <= tr.last_t):
        return False
    long = tr.side == "LONG"
    if long:
        gap_stop, gap_tp = o <= tr.stop, o >= tr.tp
        hit_stop, hit_tp = l <= tr.stop, h >= tr.tp
        tr.mfe_r = max(tr.mfe_r, (h - tr.entry) / tr.risk)
    else:
        gap_stop, gap_tp = o >= tr.stop, o <= tr.tp
        hit_stop, hit_tp = h >= tr.stop, l <= tr.tp
        tr.mfe_r = max(tr.mfe_r, (tr.entry - l) / tr.risk)
    exit_px = reason = None
    if gap_stop:  # the open tick is already beyond a level -> fills at the open
        exit_px, reason = o, _stop_reason(tr)
    elif gap_tp:
        exit_px, reason = o, "tp"
    elif hit_stop or hit_tp:
        # TradingView broker emulator path: open closer to the high -> O-H-L-C, else O-L-H-C.
        high_first = (h - o) < (o - l)
        tp_first = high_first if long else not high_first  # LONG TP is above, SHORT TP is below
        if hit_tp and (tp_first or not hit_stop):
            exit_px, reason = tr.tp, "tp"
        else:
            exit_px, reason = tr.stop, _stop_reason(tr)
    tr.last_t, tr.last_close = t, c
    if exit_px is None:
        return False
    tr.status, tr.exit_t, tr.exit_price, tr.exit_reason = "closed", t, exit_px, reason
    tr.r, tr.pct = pnl(tr, exit_px, p)
    return True


def manage(tr: Trade, bar, atr_now: float, p: Params):
    """Pine BE / trail block at the bar close (exact Pine comparisons; new stop applies from the next bar)."""
    _t, _o, h, l, c, _v = bar
    if tr.status != "open":
        return
    if tr.side == "LONG":
        if not tr.be and h - tr.entry >= p.be_at_r * tr.risk:
            tr.be = True
            tr.stop = max(tr.stop, tr.entry)
        if tr.be and finite(atr_now):
            cand = c - p.trail_atr * atr_now
            tr.trail = cand if tr.trail is None else max(tr.trail, cand)
            tr.stop = max(tr.stop, tr.trail)
    else:
        if not tr.be and tr.entry - l >= p.be_at_r * tr.risk:
            tr.be = True
            tr.stop = min(tr.stop, tr.entry)
        if tr.be and finite(atr_now):
            cand = c + p.trail_atr * atr_now
            tr.trail = cand if tr.trail is None else min(tr.trail, cand)
            tr.stop = min(tr.stop, tr.trail)


def apply_bar(tr: Trade, bar, atr_now: float, p: Params) -> bool:
    """check_exit, then manage if still open (convenience for tests / single-trade replays)."""
    if tr.status != "open" or (tr.last_t is not None and bar[0] <= tr.last_t):
        return False
    if check_exit(tr, bar, p):
        return True
    manage(tr, bar, atr_now, p)
    return False


def open_r(tr: Trade, price: float, p: Params) -> float:
    """Unrealised R at `price` (dashboard)."""
    return pnl(tr, price, p)[0]
