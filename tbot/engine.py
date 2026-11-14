"""Bar-by-bar port of the '2 Đáy/2 Đỉnh (cụm nến) → Gãy → EMA20/50 + Momentum' v4 logic (base v2 detection + v4 deltas).

Runs on CLOSED candles only, one call per closed bar, so nothing repaints. Semantics follow the Pine script:
pivots 5/5 confirmed pivRight bars later, cluster/spike validation at the pivot, double bottom/top search over the last
20 pivots, neckline = extreme between the two pivots, k2 rescan of the bars that waited for pivot confirmation, break /
cancel updates on the current close, then the signal (EMA alignment, mandatory spread, score>=minScore, ADX, HTF bias,
cooldown). v4 deltas: quality score gate, volume filter on the break bar, opposite pattern cancels, RSI optional.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from .config import Params
from .indicators import ATR, DMI, EMA, RSI, SMA, finite

NAN = float("nan")


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def quality(p1: float, p2: float, height: float, gap_bars: int, atr: float, min_bars: int, max_gap: int) -> float:
    """V4 f_quality: 0.4*symmetry + 0.3*spacing + 0.3*heightScore."""
    if not finite(atr) or atr <= 0:
        return 0.0
    symmetry = 1 - min(1.0, abs(p1 - p2) / atr)
    span = max_gap - min_bars
    mid = min_bars + span / 2
    spacing = clamp(1 - abs((gap_bars - mid) / (span / 2)), 0.0, 1.0) if span > 0 else 1.0
    height_score = clamp(height / (2.5 * atr), 0.0, 1.0)
    return 0.4 * symmetry + 0.3 * spacing + 0.3 * height_score


@dataclass
class Setup:
    side: str  # LONG / SHORT
    armed: bool = False
    broken: bool = False
    level: float = NAN  # neckline
    sl: float = NAN  # pattern low (LONG) / high (SHORT)
    exp: int = 0
    break_idx: Optional[int] = None
    meta: dict = field(default_factory=dict)

    def disarm(self):
        self.armed = False
        self.broken = False
        self.break_idx = None
        self.meta = {}


class Engine:
    """State machine for one symbol + timeframe. Absolute bar index `i` counts every bar ever processed."""

    MAX_KEEP = 300
    TRIM = 100

    def __init__(self, params: Params, symbol: str = "", tf: str = ""):
        self.p, self.symbol, self.tf = params, symbol, tf
        P = params
        self.n = 0  # number of processed bars (next absolute index)
        self.off = 0  # absolute index of list element 0
        self.t, self.o, self.h, self.l, self.c, self.v = [], [], [], [], [], []
        self.atr, self.ef, self.es, self.adx, self.vsma, self.rsi = [], [], [], [], [], []
        self._ema_f, self._ema_s, self._atr = EMA(P.ema_fast), EMA(P.ema_slow), ATR(P.atr_len)
        self._dmi, self._vsma, self._rsi = DMI(P.adx_len, P.adx_len), SMA(P.vol_len), RSI(P.rsi_len)
        self.last_up_idx = None
        self.last_dn_idx = None
        self.low_piv: list = []  # (idx, price, zoneTop)
        self.high_piv: list = []  # (idx, price, zoneBot)
        self.bull = Setup("LONG")
        self.bear = Setup("SHORT")
        self.events: list = []  # recent pattern / break events (for dashboard)

    # ---------- series access by absolute index ----------
    def _g(self, arr, i):
        return arr[i - self.off]

    @property
    def last_time(self):
        return self.t[-1] if self.t else None

    def htf_point(self):
        """(open_time, close, emaSlow) of the last processed bar (used when this engine is someone's HTF)."""
        if not self.t:
            return None
        return self.t[-1], self.c[-1], self.es[-1]

    def peek_atr(self, bar) -> float:
        """ATR value this bar WILL have once processed (used by trade trailing before the engine step)."""
        r = self._atr.rma
        tr = ATR.tr(bar[2], bar[3], self._atr.prev_close)
        if r.cnt < r.n:
            return (r.acc + tr) / r.n if r.cnt + 1 == r.n else NAN
        return r.alpha * tr + (1 - r.alpha) * r.value

    def _append(self, bar):
        t, o, h, l, c, v = bar
        self.t.append(t); self.o.append(o); self.h.append(h); self.l.append(l); self.c.append(c); self.v.append(v)
        self.ef.append(self._ema_f.update(c))
        self.es.append(self._ema_s.update(c))
        self.atr.append(self._atr.update(h, l, c))
        self.adx.append(self._dmi.update(h, l, c)[2])
        self.vsma.append(self._vsma.update(v))
        self.rsi.append(self._rsi.update(c))
        i = self.n
        self.n += 1
        if len(self.t) > self.MAX_KEEP:
            k = self.TRIM
            for arr in (self.t, self.o, self.h, self.l, self.c, self.v, self.atr, self.ef, self.es, self.adx, self.vsma, self.rsi):
                del arr[:k]
            self.off += k
        return i

    # ---------- per-bar helpers ----------
    def _vol_ok(self, i):
        if not self.p.use_volume:
            return True
        vs = self._g(self.vsma, i)
        return finite(vs) and self._g(self.v, i) >= self.p.vol_mult * vs

    def _is_pivot(self, piv, high: bool):
        P = self.p
        if piv - P.piv_left < self.off:
            return False
        arr = self.h if high else self.l
        x = self._g(arr, piv)
        for k in range(piv - P.piv_left, piv + P.piv_right + 1):
            if k == piv:
                continue
            y = self._g(arr, k)
            if (high and y >= x) or ((not high) and y <= x):
                return False
        return True

    def _cluster(self, piv, high: bool):
        """Return (valid, zone_edge) for the pivot bar, as the Pine cluster/spike block."""
        P = self.p
        a = self._g(self.atr, piv)
        if not finite(a) or a <= 0:
            return False, NAN
        arr = self.h if high else self.l
        px = self._g(arr, piv)
        tol = a * P.cluster_atr
        cnt, edge = 1, px
        for i in range(1, P.piv_left + 1):
            y = self._g(arr, piv - i)
            if (high and y >= px - tol) or ((not high) and y <= px + tol):
                cnt += 1
                edge = min(edge, y) if high else max(edge, y)
            else:
                break
        for i in range(1, P.piv_right + 1):
            y = self._g(arr, piv + i)
            if (high and y >= px - tol) or ((not high) and y <= px + tol):
                cnt += 1
                edge = min(edge, y) if high else max(edge, y)
            else:
                break
        nb = [self._g(arr, piv - i) for i in range(1, P.piv_left + 1)] + [self._g(arr, piv + i) for i in range(1, P.piv_right + 1)]
        if high:
            spike = P.spike_atr > 0 and (px - max(nb)) >= P.spike_atr * a
        else:
            spike = P.spike_atr > 0 and (min(nb) - px) >= P.spike_atr * a
        return (cnt >= P.min_cluster or spike), edge

    def _event(self, kind, side, i, **kw):
        self.events.append(dict(kind=kind, side=side, t=self._g(self.t, i), **kw))
        if len(self.events) > 20:
            del self.events[:-20]

    # ---------- main ----------
    def process_bar(self, bar, htf_dir: Optional[int] = None, cool_ok: bool = True, flat: bool = True):
        """Feed one CLOSED bar (open_time_ms, o, h, l, c, v). Returns a signal dict or None.
        htf_dir: +1 HTF close > HTF EMA50, -1 below, 0/None unknown (blocks signals when use_htf)."""
        P = self.p
        i = self._append(bar)
        c_i = self.c[-1]
        atr_i = self.atr[-1]
        ef, es = self.ef[-1], self.es[-1]
        # EMA crosses (only needed for needCross variants)
        if i - 1 >= self.off:
            pf, ps = self._g(self.ef, i - 1), self._g(self.es, i - 1)
            if all(map(finite, (ef, es, pf, ps))):
                if pf <= ps and ef > es:
                    self.last_up_idx = i
                if pf >= ps and ef < es:
                    self.last_dn_idx = i

        piv = i - P.piv_right
        if piv - P.piv_left >= self.off:
            if self._is_pivot(piv, high=False):
                ok, top = self._cluster(piv, high=False)
                if ok:
                    self._on_low(piv, top, i)
            if self._is_pivot(piv, high=True):
                ok, bot = self._cluster(piv, high=True)
                if ok:
                    self._on_high(piv, bot, i)

        self._update_setups(i, c_i, atr_i)
        return self._signal(i, c_i, atr_i, ef, es, htf_dir, cool_ok, flat)

    def _on_low(self, lidx, ltop, i):
        P = self.p
        pl = self._g(self.l, lidx)
        a_p = self._g(self.atr, lidx)  # atr[pivRight]
        a_now = self.atr[-1] if finite(self.atr[-1]) and self.atr[-1] > 0 else a_p
        tol_v, min_h = a_p * P.tol_atr, a_p * P.min_height_atr
        for (i1, p1, t1) in reversed(self.low_piv):
            gap = lidx - i1
            if gap > P.max_gap:
                break
            if gap >= P.min_bars and pl >= p1 - tol_v and i1 >= self.off:
                hh = max(self.h[i1 - self.off: lidx - self.off + 1])
                ll = min(self.l[i1 - self.off: lidx - self.off + 1])
                retrace_ok = pl <= hh - P.min_retrace * (hh - p1)
                height = hh - max(ltop, t1)
                if retrace_ok and height >= min_h and ll >= min(pl, p1) - tol_v:
                    q = quality(p1, pl, height, gap, a_p, P.min_bars, P.max_gap)
                    if q >= P.min_quality and not (self.bull.armed and self.bull.broken):
                        b = self.bull
                        b.armed, b.broken, b.break_idx = True, False, None
                        b.level, b.sl, b.exp = hh, min(pl, p1), i + P.max_bars
                        b.meta = dict(p1_t=self._g(self.t, i1), p1=p1, p2_t=self._g(self.t, lidx), p2=pl,
                                      neck=hh, sl=min(pl, p1), quality=round(q, 3), gap=gap, confirm_t=self._g(self.t, i))
                        self._event("pattern", "LONG", i, neck=hh, sl=min(pl, p1), quality=round(q, 3))
                        if P.opp_cancel:
                            self.bear.disarm()
                        for k2 in range(P.piv_right - 1, -1, -1):
                            if not b.armed:
                                break
                            bi = i - k2
                            cb = self._g(self.c, bi)
                            if cb < b.sl:
                                b.disarm()
                            elif not b.broken and cb > b.level + P.break_buf * a_now and self._vol_ok(bi):
                                self._mark_break(b, bi)
                    break
        self.low_piv.append((lidx, pl, ltop))
        if len(self.low_piv) > 20:
            self.low_piv.pop(0)

    def _on_high(self, hidx, hbot, i):
        P = self.p
        ph = self._g(self.h, hidx)
        a_p = self._g(self.atr, hidx)
        a_now = self.atr[-1] if finite(self.atr[-1]) and self.atr[-1] > 0 else a_p
        tol_v, min_h = a_p * P.tol_atr, a_p * P.min_height_atr
        for (i1, p1, b1) in reversed(self.high_piv):
            gap = hidx - i1
            if gap > P.max_gap:
                break
            if gap >= P.min_bars and ph <= p1 + tol_v and i1 >= self.off:
                hh = max(self.h[i1 - self.off: hidx - self.off + 1])
                ll = min(self.l[i1 - self.off: hidx - self.off + 1])
                retrace_ok = ph >= ll + P.min_retrace * (p1 - ll)
                height = min(hbot, b1) - ll
                if retrace_ok and height >= min_h and hh <= max(ph, p1) + tol_v:
                    q = quality(p1, ph, height, gap, a_p, P.min_bars, P.max_gap)
                    if q >= P.min_quality and not (self.bear.armed and self.bear.broken):
                        b = self.bear
                        b.armed, b.broken, b.break_idx = True, False, None
                        b.level, b.sl, b.exp = ll, max(ph, p1), i + P.max_bars
                        b.meta = dict(p1_t=self._g(self.t, i1), p1=p1, p2_t=self._g(self.t, hidx), p2=ph,
                                      neck=ll, sl=max(ph, p1), quality=round(q, 3), gap=gap, confirm_t=self._g(self.t, i))
                        self._event("pattern", "SHORT", i, neck=ll, sl=max(ph, p1), quality=round(q, 3))
                        if P.opp_cancel:
                            self.bull.disarm()
                        for k2 in range(P.piv_right - 1, -1, -1):
                            if not b.armed:
                                break
                            bi = i - k2
                            cb = self._g(self.c, bi)
                            if cb > b.sl:
                                b.disarm()
                            elif not b.broken and cb < b.level - P.break_buf * a_now and self._vol_ok(bi):
                                self._mark_break(b, bi)
                    break
        self.high_piv.append((hidx, ph, hbot))
        if len(self.high_piv) > 20:
            self.high_piv.pop(0)

    def _mark_break(self, s: Setup, bi):
        s.broken, s.break_idx = True, bi
        s.meta["break_t"] = self._g(self.t, bi)
        self._event("break", s.side, bi, neck=s.level)

    def _update_setups(self, i, c, a):
        P = self.p
        b = self.bull
        if b.armed:
            if c < b.sl:
                b.disarm()
            elif b.broken:
                if P.cross_win > 0 and i - b.break_idx > P.cross_win:
                    b.disarm()
                elif P.cancel_retest and c < b.level:
                    b.disarm()
            else:
                if i > b.exp:
                    b.disarm()
                elif finite(a) and c > b.level + P.break_buf * a and self._vol_ok(i):
                    self._mark_break(b, i)
        s = self.bear
        if s.armed:
            if c > s.sl:
                s.disarm()
            elif s.broken:
                if P.cross_win > 0 and i - s.break_idx > P.cross_win:
                    s.disarm()
                elif P.cancel_retest and c > s.level:
                    s.disarm()
            else:
                if i > s.exp:
                    s.disarm()
                elif finite(a) and c < s.level - P.break_buf * a and self._vol_ok(i):
                    self._mark_break(s, i)

    def _signal(self, i, c, a, ef, es, htf_dir, cool_ok, flat):
        P = self.p
        if not (finite(a) and a > 0 and finite(ef) and finite(es)):
            return None
        spread = ef - es
        prev_spread = NAN
        if i - 1 >= self.off:
            pf, ps = self._g(self.ef, i - 1), self._g(self.es, i - 1)
            if finite(pf) and finite(ps):
                prev_spread = pf - ps
        slope = NAN
        if i - P.slope_len >= self.off:
            e0 = self._g(self.ef, i - P.slope_len)
            if finite(e0):
                slope = (ef - e0) / a
        score_up = int(finite(slope) and slope >= P.slope_atr) + int(finite(prev_spread) and spread > prev_spread) + int(c > max(ef, es))
        score_dn = int(finite(slope) and slope <= -P.slope_atr) + int(finite(prev_spread) and spread < prev_spread) + int(c < min(ef, es))
        adx = self.adx[-1]
        adx_ok = P.adx_min <= 0 or (finite(adx) and adx >= P.adx_min)
        rsi = self.rsi[-1]
        base = cool_ok and flat and adx_ok

        def cross_ok(last_idx, brk):
            if not P.need_cross:
                return True
            if last_idx is None:
                return False
            win = P.confirm_win == 0 or i - last_idx <= P.confirm_win
            return last_idx > brk and win

        b, s = self.bull, self.bear
        long_sig = (base and b.armed and b.broken and b.break_idx is not None and i >= b.break_idx and ef > es
                    and spread >= P.spread_atr * a and score_up >= P.min_score
                    and (not P.use_htf or htf_dir == 1)
                    and (not P.use_rsi or (finite(rsi) and rsi <= P.rsi_long_max))
                    and cross_ok(self.last_up_idx, b.break_idx))
        short_sig = (base and s.armed and s.broken and s.break_idx is not None and i >= s.break_idx and ef < es
                     and -spread >= P.spread_atr * a and score_dn >= P.min_score
                     and (not P.use_htf or htf_dir == -1)
                     and (not P.use_rsi or (finite(rsi) and rsi >= P.rsi_short_min))
                     and cross_ok(self.last_dn_idx, s.break_idx))
        out = None
        if long_sig:
            out = self._make_signal("LONG", b, i, c, a, score_up, adx, ef, es)
            b.disarm()
        if short_sig:
            sig = self._make_signal("SHORT", s, i, c, a, score_dn, adx, ef, es)
            s.disarm()
            out = out or sig  # both on one bar is practically impossible (EMA alignment differs)
        return out

    def _make_signal(self, side, s: Setup, i, c, a, score, adx, ef, es):
        return dict(symbol=self.symbol, tf=self.tf, side=side, t=self._g(self.t, i), entry=c, atr=a,
                    pattern_sl=s.sl, neck=s.level, score=score, adx=adx, ema_fast=ef, ema_slow=es,
                    quality=s.meta.get("quality"), pattern=dict(s.meta))

    def armed_state(self):
        out = []
        for s in (self.bull, self.bear):
            if s.armed:
                out.append(dict(side=s.side, broken=s.broken, neck=s.level, sl=s.sl,
                                quality=s.meta.get("quality"), p2_t=s.meta.get("p2_t"), break_t=s.meta.get("break_t")))
        return out
