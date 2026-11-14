"""Straightforward, independent reference simulation of script_v4_official.pine (strategy, Active preset) on full
arrays, written line-by-line from the Pine source (series[k] indexing, bar_index, strategy broker emulation with
process_orders_on_close, pyramiding=0). Used only by the parity test. Broker emulation: stop/limit orders placed at a
bar close are checked from the next bar: the open tick first (an order whose level the open already passed fills at
the open), then TradingView's documented intrabar path (open closer to the high -> O-H-L-C, otherwise O-L-H-C); stop
fills at the stop, limit at the limit."""
import math

NA = float("nan")


def isna(x):
    return x is None or (isinstance(x, float) and math.isnan(x))


def sma_seed(vals, n, alpha):
    out, acc, cnt, prev = [], 0.0, 0, NA
    for v in vals:
        if isna(v):
            out.append(prev)
            continue
        if cnt < n:
            acc += v
            cnt += 1
            prev = acc / n if cnt == n else NA
        else:
            prev = alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def ema(x, n):
    return sma_seed(x, n, 2.0 / (n + 1))


def rma(x, n):
    return sma_seed(x, n, 1.0 / n)


def sma(x, n):
    return [sum(x[i - n + 1:i + 1]) / n if i >= n - 1 else NA for i in range(len(x))]


def simulate(bars, P, htf=None):
    """bars: list of (t,o,h,l,c,v). htf: list of +1/-1/0 per bar (HTF bias), or None. Returns (signals, trades)."""
    t = [b[0] for b in bars]; o = [b[1] for b in bars]; high = [b[2] for b in bars]
    low = [b[3] for b in bars]; close = [b[4] for b in bars]; volume = [b[5] for b in bars]
    n = len(bars)
    emaF, emaS = ema(close, P.ema_fast), ema(close, P.ema_slow)
    tr = [high[0] - low[0]] + [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])) for i in range(1, n)]
    atr = rma(tr, P.atr_len)
    volSma = sma(volume, P.vol_len)
    # ta.dmi
    pdm, mdm, trn = [NA], [NA], [NA]
    for i in range(1, n):
        up, dn = high[i] - high[i - 1], low[i - 1] - low[i]
        pdm.append(up if (up > dn and up > 0) else 0.0)
        mdm.append(dn if (dn > up and dn > 0) else 0.0)
        trn.append(tr[i])
    trur, sp, sm = rma(trn, P.adx_len), rma(pdm, P.adx_len), rma(mdm, P.adx_len)
    dx = []
    for i in range(n):
        if isna(trur[i]) or trur[i] == 0:
            dx.append(NA); continue
        pl_, mi_ = 100 * sp[i] / trur[i], 100 * sm[i] / trur[i]
        dx.append(0.0 if pl_ + mi_ == 0 else 100 * abs(pl_ - mi_) / (pl_ + mi_))
    adx = rma(dx, P.adx_len)

    def S(arr, i, k):  # series[k] at bar i
        j = i - k
        return arr[j] if 0 <= j < n else NA

    def fq(p1, p2, height, gap, a):
        a2 = a + 1e-10
        sym = 1.0 - min(1.0, abs(p1 - p2) / a2)
        span = P.max_gap - P.min_bars
        mid = P.min_bars + span / 2.0
        spc = max(0.0, min(1.0, 1.0 - abs((gap - mid) / (span / 2.0 + 1e-10))))
        hs = max(0.0, min(1.0, height / (2.5 * a2)))
        return 0.4 * sym + 0.3 * spc + 0.3 * hs

    lowIdx, lowPx, lowTop, highIdx, highPx, highBot = [], [], [], [], [], []
    bull = dict(armed=False, broken=False, level=NA, sl=NA, exp=None, bidx=None)
    bear = dict(armed=False, broken=False, level=NA, sl=NA, exp=None, bidx=None)
    lastExitBar = -99999
    pos = None  # dict(side, entry, stop, tp, risk, be, trailOn, entry_i)
    signals, trades = [], []
    L, R = P.piv_left, P.piv_right

    for i in range(n):
        # ---- broker: orders from previous close act on this bar
        exited = False
        if pos is not None and i > pos["entry_i"]:
            if pos["side"] == "LONG":
                if o[i] <= pos["stop"] or o[i] >= pos["tp"]:  # open already beyond a level: fills at the open
                    px = o[i]
                elif low[i] <= pos["stop"] or high[i] >= pos["tp"]:
                    hi_first = (high[i] - o[i]) < (o[i] - low[i])  # TV path: O-H-L-C if open nearer the high
                    if high[i] >= pos["tp"] and (hi_first or low[i] > pos["stop"]):
                        px = pos["tp"]
                    else:
                        px = pos["stop"]
                else:
                    px = None
            else:
                if o[i] >= pos["stop"] or o[i] <= pos["tp"]:
                    px = o[i]
                elif high[i] >= pos["stop"] or low[i] <= pos["tp"]:
                    hi_first = (high[i] - o[i]) < (o[i] - low[i])
                    if low[i] <= pos["tp"] and (not hi_first or high[i] < pos["stop"]):
                        px = pos["tp"]
                    else:
                        px = pos["stop"]
                else:
                    px = None
            if px is not None:
                trades.append(dict(side=pos["side"], entry_t=t[pos["entry_i"]], entry=pos["entry"], stop0=pos["stop0"],
                                   tp=pos["tp"], exit_t=t[i], exit=px))
                pos = None
                exited = True
        a = atr[i]
        # ---- pivots: TradingView ta.pivothigh/low = candidate is the LAST index of the window max/min
        ph = pl = NA
        if i - R - L >= 0:
            c_ = i - R
            win_h, win_l = high[c_ - L:i + 1], low[c_ - L:i + 1]
            if len(win_h) - 1 - win_h[::-1].index(max(win_h)) == L:
                ph = high[c_]
            if len(win_l) - 1 - win_l[::-1].index(min(win_l)) == L:
                pl = low[c_]
        validH = validL = False
        hIdx = lIdx = None
        hBot = lTop = NA
        aP = S(atr, i, R)
        if not isna(ph) and not isna(aP):
            tolP = aP * P.cluster_atr; cnt = 1; zBot = ph
            for k in range(1, L + 1):
                v = S(high, i, R + k)
                if v >= ph - tolP: cnt += 1; zBot = min(zBot, v)
                else: break
            for k in range(1, R + 1):
                v = S(high, i, R - k)
                if v >= ph - tolP: cnt += 1; zBot = min(zBot, v)
                else: break
            nb = max([S(high, i, R + k) for k in range(1, L + 1)] + [S(high, i, R - k) for k in range(1, R + 1)])
            spike = P.spike_atr > 0 and (ph - nb) >= P.spike_atr * aP
            hIdx, hBot, validH = i - R, zBot, cnt >= P.min_cluster or spike
        if not isna(pl) and not isna(aP):
            tolP = aP * P.cluster_atr; cnt = 1; zTop = pl
            for k in range(1, L + 1):
                v = S(low, i, R + k)
                if v <= pl + tolP: cnt += 1; zTop = max(zTop, v)
                else: break
            for k in range(1, R + 1):
                v = S(low, i, R - k)
                if v <= pl + tolP: cnt += 1; zTop = max(zTop, v)
                else: break
            nb = min([S(low, i, R + k) for k in range(1, L + 1)] + [S(low, i, R - k) for k in range(1, R + 1)])
            spike = P.spike_atr > 0 and (nb - pl) >= P.spike_atr * aP
            lIdx, lTop, validL = i - R, zTop, cnt >= P.min_cluster or spike

        def volok_at(k):
            return (not P.use_volume) or (not isna(S(volSma, i, k)) and S(volume, i, k) >= P.vol_mult * S(volSma, i, k))

        # ---- 2 day
        if validL:
            tolV, minH = aP * P.tol_atr, aP * P.min_height_atr
            for j in range(len(lowIdx) - 1, -1, -1):
                i1, p1, t1 = lowIdx[j], lowPx[j], lowTop[j]
                gap = lIdx - i1
                if gap > P.max_gap:
                    break
                if gap >= P.min_bars and pl >= p1 - tolV:
                    hh = max(S(high, i, k) for k in range(i - lIdx, i - i1 + 1))
                    ll = min(S(low, i, k) for k in range(i - lIdx, i - i1 + 1))
                    retr = pl <= hh - P.min_retrace * (hh - p1)
                    hL = hh - max(lTop, t1)
                    if retr and hL >= minH and ll >= min(pl, p1) - tolV:
                        q = fq(p1, pl, hL, gap, aP)
                        if q >= P.min_quality and not (bull["armed"] and bull["broken"]):
                            bull.update(armed=True, broken=False, bidx=None, level=hh, sl=min(pl, p1), exp=i + P.max_bars, q=q)
                            if P.opp_cancel:
                                bear.update(armed=False, broken=False)
                            for k2 in range(R - 1, -1, -1):
                                if bull["armed"]:
                                    if S(close, i, k2) < bull["sl"]:
                                        bull.update(armed=False, broken=False)
                                    elif not bull["broken"] and S(close, i, k2) > bull["level"] + P.break_buf * a and volok_at(k2):
                                        bull.update(broken=True, bidx=i - k2)
                        break  # Pine: inside the retrace/height block (line 278 / 330)
            lowIdx.append(lIdx); lowPx.append(pl); lowTop.append(lTop)
            if len(lowIdx) > 20:
                lowIdx.pop(0); lowPx.pop(0); lowTop.pop(0)
        # ---- 2 dinh
        if validH:
            tolV, minH = aP * P.tol_atr, aP * P.min_height_atr
            for j in range(len(highIdx) - 1, -1, -1):
                i1, p1, b1 = highIdx[j], highPx[j], highBot[j]
                gap = hIdx - i1
                if gap > P.max_gap:
                    break
                if gap >= P.min_bars and ph <= p1 + tolV:
                    hh = max(S(high, i, k) for k in range(i - hIdx, i - i1 + 1))
                    ll = min(S(low, i, k) for k in range(i - hIdx, i - i1 + 1))
                    retr = ph >= ll + P.min_retrace * (p1 - ll)
                    hH = min(hBot, b1) - ll
                    if retr and hH >= minH and hh <= max(ph, p1) + tolV:
                        q = fq(p1, ph, hH, gap, aP)
                        if q >= P.min_quality and not (bear["armed"] and bear["broken"]):
                            bear.update(armed=True, broken=False, bidx=None, level=ll, sl=max(ph, p1), exp=i + P.max_bars, q=q)
                            if P.opp_cancel:
                                bull.update(armed=False, broken=False)
                            for k2 in range(R - 1, -1, -1):
                                if bear["armed"]:
                                    if S(close, i, k2) > bear["sl"]:
                                        bear.update(armed=False, broken=False)
                                    elif not bear["broken"] and S(close, i, k2) < bear["level"] - P.break_buf * a and volok_at(k2):
                                        bear.update(broken=True, bidx=i - k2)
                        break
            highIdx.append(hIdx); highPx.append(ph); highBot.append(hBot)
            if len(highIdx) > 20:
                highIdx.pop(0); highPx.pop(0); highBot.pop(0)
        # ---- update break / cancel
        c = close[i]
        volOk = volok_at(0)
        for st, sgn in ((bull, 1), (bear, -1)):
            if not st["armed"]:
                continue
            if (c < st["sl"]) if sgn == 1 else (c > st["sl"]):
                st.update(armed=False, broken=False)
            elif st["broken"]:
                if P.cross_win > 0 and i - st["bidx"] > P.cross_win:
                    st.update(armed=False, broken=False)
                elif P.cancel_retest and ((c < st["level"]) if sgn == 1 else (c > st["level"])):
                    st.update(armed=False, broken=False)
            else:
                if i > st["exp"]:
                    st["armed"] = False
                elif not isna(a) and ((c > st["level"] + P.break_buf * a) if sgn == 1 else (c < st["level"] - P.break_buf * a)) and volOk:
                    st.update(broken=True, bidx=i)
        # ---- signals
        ef, es = emaF[i], emaS[i]
        ok = not (isna(a) or isna(ef) or isna(es))
        spread = ef - es if ok else NA
        prev = (emaF[i - 1] - emaS[i - 1]) if i >= 1 and not isna(emaF[i - 1]) and not isna(emaS[i - 1]) else NA
        slope = (ef - emaF[i - P.slope_len]) / a if ok and i >= P.slope_len and not isna(emaF[i - P.slope_len]) else NA
        sU = (not isna(slope) and slope >= P.slope_atr) + (not isna(prev) and ok and spread > prev) + (ok and c > max(ef, es))
        sD = (not isna(slope) and slope <= -P.slope_atr) + (not isna(prev) and ok and spread < prev) + (ok and c < min(ef, es))
        adxOk = P.adx_min <= 0 or (not isna(adx[i]) and adx[i] >= P.adx_min)
        coolOk = i - lastExitBar >= P.cooldown
        hd = htf[i] if htf is not None else 0
        htfL = (not P.use_htf) or hd == 1
        htfS = (not P.use_htf) or hd == -1
        longSig = ok and coolOk and bull["armed"] and bull["broken"] and bull["bidx"] is not None and i >= bull["bidx"] and ef > es and spread >= P.spread_atr * a and sU >= P.min_score and adxOk and htfL
        shortSig = ok and coolOk and bear["armed"] and bear["broken"] and bear["bidx"] is not None and i >= bear["bidx"] and ef < es and -spread >= P.spread_atr * a and sD >= P.min_score and adxOk and htfS
        flat = pos is None
        if longSig and flat:
            stp = max(bull["sl"], c - P.stop_cap_atr * a) if P.stop_cap_atr > 0 else bull["sl"]
            if c - stp > 0:
                pos = dict(side="LONG", entry=c, stop=stp, stop0=stp, tp=c + P.tp_r * (c - stp), risk=c - stp, be=False, trailOn=False, entry_i=i)
                bull.update(armed=False, broken=False)
                signals.append((t[i], "LONG"))
        if shortSig and flat and pos is None:
            stp = min(bear["sl"], c + P.stop_cap_atr * a) if P.stop_cap_atr > 0 else bear["sl"]
            if stp - c > 0:
                pos = dict(side="SHORT", entry=c, stop=stp, stop0=stp, tp=c - P.tp_r * (stp - c), risk=stp - c, be=False, trailOn=False, entry_i=i)
                bear.update(armed=False, broken=False)
                signals.append((t[i], "SHORT"))
        # ---- BE / trail for a position held through this bar (not the entry bar)
        if pos is not None and i > pos["entry_i"]:
            e, rk = pos["entry"], pos["risk"]
            if pos["side"] == "LONG":
                if not pos["be"] and high[i] - e >= rk:
                    pos["be"] = True; pos["stop"] = max(pos["stop"], e)
                if high[i] - e >= rk:
                    pos["trailOn"] = True
                if pos["trailOn"]:
                    pos["stop"] = max(pos["stop"], c - P.trail_atr * a)
            else:
                if not pos["be"] and e - low[i] >= rk:
                    pos["be"] = True; pos["stop"] = min(pos["stop"], e)
                if e - low[i] >= rk:
                    pos["trailOn"] = True
                if pos["trailOn"]:
                    pos["stop"] = min(pos["stop"], c + P.trail_atr * a)
        if exited:
            lastExitBar = i
    return signals, trades
