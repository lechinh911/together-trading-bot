"""Pattern / break / signal engine: a bar-by-bar port of the official Pine
`2 Day/2 Dinh v4 -> Break -> EMA Momentum` (together_bot_spec/script_v4_official.pine), Active preset.

One `Engine` = one symbol + timeframe. `process_bar` is called once per CLOSED candle (never the forming one), so
nothing repaints. Per bar, in the Pine order:
  1. indicators: EMA20/50, ATR14 (RMA), DMI/ADX14, SMA20(volume), RSI14 (RSI only used if use_rsi);
  2. pivots 5/5 (`ta.pivotlow/pivothigh`, confirmed piv_right bars later) + cluster/spike validation at the pivot;
  3. "2 ĐÁY" search over the last 20 low pivots, then "2 ĐỈNH" over the last 20 high pivots (arm setup, opposite
     pattern cancels, k2 rescan of the bars that waited for the pivot confirmation);
  4. break / cancel update of the armed setups on this close;
  5. signal: coolOk, flat, EMA side, mandatory spread, momentum score, ADX, HTF bias -> entry at this close.
Position management (stop/TP/BE/trail, cooldown bookkeeping) lives in strategy.py / trades.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .config import Params
from .indicators import ATR, DMI, EMA, RSI, SMA, finite

NAN = float("nan")
LONG, SHORT = "LONG", "SHORT"


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def quality(p1: float, p2: float, height: float, gap_bars: int, atr: float, min_bars: int, max_gap: int) -> float:
    """Pine f_quality: 0.4*symmetry + 0.3*spacing + 0.3*heightScore (atrSafe = atr + 1e-10)."""
    if not finite(atr):
        return 0.0
    a = atr + 1e-10
    symmetry = 1 - min(1.0, abs(p1 - p2) / a)
    span = max_gap - min_bars
    mid = min_bars + span / 2
    spacing = clamp(1 - abs((gap_bars - mid) / (span / 2 + 1e-10)), 0.0, 1.0)
    height_score = clamp(height / (2.5 * a), 0.0, 1.0)
    return 0.4 * symmetry + 0.3 * spacing + 0.3 * height_score


@dataclass
class Setup:
    """Pine bullArmed/bullBroken/bullLevel/bullSL/bullExp/bullBreakIdx (or the bear twin)."""
    side: str  # LONG (2 đáy) / SHORT (2 đỉnh)
    armed: bool = False
    broken: bool = False
    level: float = NAN  # neckline
    sl: float = NAN  # pattern low (LONG) / pattern high (SHORT)
    exp: int = 0  # last bar index at which an unbroken setup may still break
    break_idx: Optional[int] = None
    meta: dict = field(default_factory=dict)  # display info (pivot times/prices, quality, break time)

    @property
    def sign(self) -> int:
        return 1 if self.side == LONG else -1

    def disarm(self):
        self.armed = False
        self.broken = False
        self.break_idx = None
        self.meta = {}


class Engine:
    """State machine for one symbol + timeframe. Absolute bar index `i` (= Pine bar_index) counts every processed bar;
    only the last MAX_KEEP bars of each series are kept (Pine needs at most ~110 bars back)."""

    MAX_KEEP = 300
    TRIM = 100
    MAX_PIVOTS = 20  # Pine keeps the last 20 pivots per side

    def __init__(self, params: Params, symbol: str = "", tf: str = "", hist=None):
        """hist: optional htf.CloseEmaSeries kept up to date with this TF's closes (set for the 4h HTF engine)."""
        self.p, self.symbol, self.tf = params, symbol, tf
        self.hist = hist
        P = params
        self.n = 0  # number of processed bars (= bar_index of the next bar)
        self.off = 0  # absolute index of list element 0
        self.t, self.o, self.h, self.l, self.c, self.v = [], [], [], [], [], []
        self.atr, self.ef, self.es, self.adx, self.vsma, self.rsi = [], [], [], [], [], []
        self._ema_f, self._ema_s, self._atr = EMA(P.ema_fast), EMA(P.ema_slow), ATR(P.atr_len)
        self._dmi, self._vsma, self._rsi = DMI(P.adx_len, P.adx_len), SMA(P.vol_len), RSI(P.rsi_len)
        self.last_up_idx = None  # last EMA cross up / down (only used when need_cross=True)
        self.last_dn_idx = None
        self.low_piv: list = []  # Pine lowIdx/lowPx/lowTop: (idx, price, zoneTop)
        self.high_piv: list = []  # Pine highIdx/highPx/highBot: (idx, price, zoneBot)
        self.bull = Setup(LONG)
        self.bear = Setup(SHORT)

    # ---------- series access by absolute index ----------
    def _g(self, arr, i):
        return arr[i - self.off]

    def _append(self, bar):
        t, o, h, l, c, v = bar
        self.t.append(t); self.o.append(o); self.h.append(h); self.l.append(l); self.c.append(c); self.v.append(v)
        self.ef.append(self._ema_f.update(c))
        self.es.append(self._ema_s.update(c))
        self.atr.append(self._atr.update(h, l, c))
        self.adx.append(self._dmi.update(h, l, c)[2])
        self.vsma.append(self._vsma.update(v))
        self.rsi.append(self._rsi.update(c))
        if self.hist is not None:
            self.hist.add(t, c)
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
        """Pine volOk at bar i: volume >= volMult * SMA20(volume) (na SMA -> False)."""
        if not self.p.use_volume:
            return True
        vs = self._g(self.vsma, i)
        return finite(vs) and self._g(self.v, i) >= self.p.vol_mult * vs

    def _is_pivot(self, piv, high: bool):
        """ta.pivothigh/pivotlow(5, 5) with TradingView's tie rule: no LEFT bar strictly beyond the candidate (equal
        allowed) and every RIGHT bar strictly inside it (equal rejects). A plateau therefore gives one pivot, at its
        last bar - identical to 'last index of the window maximum is the candidate'."""
        P = self.p
        if piv - P.piv_left < self.off:
            return False
        arr = self.h if high else self.l
        x = self._g(arr, piv)
        for k in range(piv - P.piv_left, piv):
            y = self._g(arr, k)
            if (y > x) if high else (y < x):
                return False
        for k in range(piv + 1, piv + P.piv_right + 1):
            y = self._g(arr, k)
            if (y >= x) if high else (y <= x):
                return False
        return True

    def _cluster(self, piv, high: bool):
        """Pine validH/validL block: returns (valid, zone_edge). Cluster = consecutive neighbours within
        clusterAtr*ATR[pivot] of the pivot (stop at the first miss, each side); valid if >= minCluster bars or the
        pivot sticks out >= spikeAtr*ATR beyond all 10 neighbours. zone_edge = hBot (high) / lTop (low)."""
        P = self.p
        a = self._g(self.atr, piv)
        if not finite(a) or a <= 0:
            return False, NAN
        arr = self.h if high else self.l
        px = self._g(arr, piv)
        tol = a * P.cluster_atr

        def near(y):
            return y >= px - tol if high else y <= px + tol

        cnt, edge = 1, px
        for step in (-1, 1):  # left side, then right side
            for k in range(1, (P.piv_left if step < 0 else P.piv_right) + 1):
                y = self._g(arr, piv + step * k)
                if not near(y):
                    break
                cnt += 1
                edge = min(edge, y) if high else max(edge, y)
        nb = [self._g(arr, piv - k) for k in range(1, P.piv_left + 1)] + [self._g(arr, piv + k) for k in range(1, P.piv_right + 1)]
        stick_out = (px - max(nb)) if high else (min(nb) - px)
        spike = P.spike_atr > 0 and stick_out >= P.spike_atr * a
        return (cnt >= P.min_cluster or spike), edge

    # ---------- main ----------
    def process_bar(self, bar, htf_dir: Optional[int] = None, cool_ok: bool = True, flat: bool = True):
        """Feed one CLOSED bar (open_time_ms, o, h, l, c, v). Returns a signal dict (entry is placed by the caller)
        or None. htf_dir: +1 HTF close > HTF EMA50, -1 below, 0/None unknown (blocks signals when use_htf).
        cool_ok / flat: Pine coolOk and strategy.position_size == 0, supplied by PairStrategy."""
        P = self.p
        i = self._append(bar)
        c_i, atr_i = self.c[-1], self.atr[-1]
        ef, es = self.ef[-1], self.es[-1]
        if i - 1 >= self.off:  # ta.crossover / crossunder (only matter with need_cross=True)
            pf, ps = self._g(self.ef, i - 1), self._g(self.es, i - 1)
            if all(map(finite, (ef, es, pf, ps))):
                if pf <= ps and ef > es:
                    self.last_up_idx = i
                if pf >= ps and ef < es:
                    self.last_dn_idx = i

        piv = i - P.piv_right  # candidate pivot bar confirmed on this bar
        if piv - P.piv_left >= self.off:
            valid_l = valid_h = False
            if self._is_pivot(piv, high=False):
                valid_l, l_top = self._cluster(piv, high=False)
            if self._is_pivot(piv, high=True):
                valid_h, h_bot = self._cluster(piv, high=True)
            if valid_l:  # ---- 2 ĐÁY (Pine runs it before 2 ĐỈNH)
                self._on_pivot(self.bull, piv, l_top, i)
            if valid_h:  # ---- 2 ĐỈNH
                self._on_pivot(self.bear, piv, h_bot, i)

        self._update_setup(self.bull, i, c_i, atr_i)
        self._update_setup(self.bear, i, c_i, atr_i)
        return self._signal(i, c_i, atr_i, ef, es, htf_dir, cool_ok, flat)

    def _on_pivot(self, s: Setup, pidx, edge, i):
        """Pine '2 DAY' (s=bull, low pivot) / '2 DINH' (s=bear, high pivot) block for the pivot confirmed at bar i.
        Walk back over earlier pivots of the same kind (newest first, stop beyond maxGap); the first one that passes
        spacing + tolerance + retrace + height + containment ends the search (armed only if quality >= minQuality and
        the setup is not already armed-and-broken)."""
        P = self.p
        bull = s.sign == 1
        pivots = self.low_piv if bull else self.high_piv
        px = self._g(self.l if bull else self.h, pidx)
        a_p = self._g(self.atr, pidx)  # atr[pivRight]
        a_now = self.atr[-1] if finite(self.atr[-1]) and self.atr[-1] > 0 else a_p  # atr (current bar) for k2 rescan
        tol_v, min_h = a_p * P.tol_atr, a_p * P.min_height_atr
        for (i1, p1, e1) in reversed(pivots):
            gap = pidx - i1
            if gap > P.max_gap:
                break
            within = (px >= p1 - tol_v) if bull else (px <= p1 + tol_v)  # 2nd pivot not far beyond the 1st
            if not (gap >= P.min_bars and within and i1 >= self.off):
                continue
            hh = max(self.h[i1 - self.off: pidx - self.off + 1])
            ll = min(self.l[i1 - self.off: pidx - self.off + 1])
            if bull:
                retrace_ok = px <= hh - P.min_retrace * (hh - p1)
                height = hh - max(edge, e1)
                contained = ll >= min(px, p1) - tol_v
                level, sl = hh, min(px, p1)
            else:
                retrace_ok = px >= ll + P.min_retrace * (p1 - ll)
                height = min(edge, e1) - ll
                contained = hh <= max(px, p1) + tol_v
                level, sl = ll, max(px, p1)
            if not (retrace_ok and height >= min_h and contained):
                continue
            q = quality(p1, px, height, gap, a_p, P.min_bars, P.max_gap)
            if q >= P.min_quality and not (s.armed and s.broken):
                self._arm(s, level, sl, q, i1, p1, pidx, px, gap, i, a_now)
            break
        pivots.append((pidx, px, edge))
        if len(pivots) > self.MAX_PIVOTS:
            pivots.pop(0)

    def _arm(self, s: Setup, level, sl, q, i1, p1, pidx, px, gap, i, a_now):
        """Arm the setup, cancel the opposite one (oppCancel), then the Pine k2 rescan: bars i-4..i are re-checked for
        invalidation (close beyond pattern SL) or a break (close beyond neckline + breakBuf*ATR(now) with volume)."""
        P = self.p
        s.armed, s.broken, s.break_idx = True, False, None
        s.level, s.sl, s.exp = level, sl, i + P.max_bars
        s.meta = dict(p1_t=self._g(self.t, i1), p1=p1, p2_t=self._g(self.t, pidx), p2=px, neck=level, sl=sl,
                      quality=round(q, 3), gap=gap, confirm_t=self._g(self.t, i))
        if P.opp_cancel:
            (self.bear if s.sign == 1 else self.bull).disarm()
        for k2 in range(P.piv_right - 1, -1, -1):
            if not s.armed:
                break
            bi = i - k2
            cb = self._g(self.c, bi)
            if s.sign * (cb - s.sl) < 0:
                s.disarm()
            elif not s.broken and self._breaks(s, cb, a_now) and self._vol_ok(bi):
                self._mark_break(s, bi)

    def _breaks(self, s: Setup, c, a) -> bool:
        """Close beyond the neckline by breakBuf*ATR (exact Pine expression per side)."""
        if s.sign == 1:
            return c > s.level + self.p.break_buf * a
        return c < s.level - self.p.break_buf * a

    def _mark_break(self, s: Setup, bi):
        s.broken, s.break_idx = True, bi
        s.meta["break_t"] = self._g(self.t, bi)

    def _update_setup(self, s: Setup, i, c, a):
        """Pine 'UPDATE BREAK / CANCEL' for one side on this close."""
        P = self.p
        if not s.armed:
            return
        if s.sign * (c - s.sl) < 0:  # close back beyond the pattern extreme -> invalid
            s.disarm()
        elif s.broken:
            if P.cross_win > 0 and i - s.break_idx > P.cross_win:
                s.disarm()
            elif P.cancel_retest and s.sign * (c - s.level) < 0:
                s.disarm()
        elif i > s.exp:  # waited more than maxBars for the break
            s.disarm()
        elif finite(a) and self._breaks(s, c, a) and self._vol_ok(i):
            self._mark_break(s, i)

    def _signal(self, i, c, a, ef, es, htf_dir, cool_ok, flat):
        """Pine SIGNALS + entry gate. Returns the signal of the entry that Pine would place on this close."""
        P = self.p
        if not (finite(a) and a > 0 and finite(ef) and finite(es)):
            return None
        spread = ef - es
        prev_spread = NAN
        if i - 1 >= self.off:
            pf, ps = self._g(self.ef, i - 1), self._g(self.es, i - 1)
            if finite(pf) and finite(ps):
                prev_spread = pf - ps
        slope = NAN  # (emaF - emaF[slopeLen]) / atr
        if i - P.slope_len >= self.off:
            e0 = self._g(self.ef, i - P.slope_len)
            if finite(e0):
                slope = (ef - e0) / a
        score_up = int(finite(slope) and slope >= P.slope_atr) + int(finite(prev_spread) and spread > prev_spread) + int(c > max(ef, es))
        score_dn = int(finite(slope) and slope <= -P.slope_atr) + int(finite(prev_spread) and spread < prev_spread) + int(c < min(ef, es))
        adx = self.adx[-1]
        adx_ok = P.adx_min <= 0 or (finite(adx) and adx >= P.adx_min)
        rsi = self.rsi[-1]
        if not (cool_ok and flat and adx_ok):
            return None

        def cross_ok(last_idx, brk):
            if not P.need_cross:
                return True
            if last_idx is None:
                return False
            win = P.confirm_win == 0 or i - last_idx <= P.confirm_win
            return last_idx > brk and win

        b, s = self.bull, self.bear
        long_sig = (b.armed and b.broken and b.break_idx is not None and i >= b.break_idx and ef > es
                    and spread >= P.spread_atr * a and score_up >= P.min_score
                    and (not P.use_htf or htf_dir == 1)
                    and (not P.use_rsi or (finite(rsi) and rsi <= P.rsi_long_max))
                    and cross_ok(self.last_up_idx, b.break_idx))
        short_sig = (s.armed and s.broken and s.break_idx is not None and i >= s.break_idx and ef < es
                     and -spread >= P.spread_atr * a and score_dn >= P.min_score
                     and (not P.use_htf or htf_dir == -1)
                     and (not P.use_rsi or (finite(rsi) and rsi >= P.rsi_short_min))
                     and cross_ok(self.last_dn_idx, s.break_idx))
        # long and short are mutually exclusive (EMA side). Pine enters only if risk > 0 and resets the setup only
        # when the entry is actually placed.
        for ok, st, score in ((long_sig, b, score_up), (short_sig, s, score_dn)):
            if not ok:
                continue
            stop = st.sl
            if P.stop_cap_atr > 0:  # SL capped at stopCap*ATR from the entry
                stop = max(st.sl, c - P.stop_cap_atr * a) if st.sign == 1 else min(st.sl, c + P.stop_cap_atr * a)
            if st.sign * (c - stop) > 0:
                out = dict(symbol=self.symbol, tf=self.tf, side=st.side, t=self._g(self.t, i), entry=c, atr=a,
                           pattern_sl=st.sl, neck=st.level, stop=stop, score=score, adx=adx, ema_fast=ef, ema_slow=es,
                           quality=st.meta.get("quality"), pattern=dict(st.meta))
                st.disarm()
                return out
        return None

    def armed_state(self):
        """Armed setups for the dashboard ('waiting' list and setup counters)."""
        return [dict(side=s.side, broken=s.broken, neck=s.level, sl=s.sl, quality=s.meta.get("quality"),
                     p2_t=s.meta.get("p2_t"), break_t=s.meta.get("break_t"))
                for s in (self.bull, self.bear) if s.armed]
