"""Regression cases from the live bot vs the owner's TradingView chart (official v4 Pine, Active preset), 2026-11-15.
Real Binance USDT-M klines: 5m chart + 4h HTF (htfTf="240").
  - SIRENUSDT 5m, bar 01:20-01:25 UTC+7: 2-đỉnh setup armed and broken ('Gãy HT' label) but NO order on TradingView:
    the last confirmed 4h bar (19:00-23:00 UTC+7) closed 0.02651 above its EMA50 (~0.02645) -> htfBear false.
    (The old bot used a 1h HTF and opened a SHORT here.)
  - WOOUSDT 5m, bar 01:25-01:30 UTC+7: SHORT at 0.00933 that TradingView shows. The old bot missed it because its
    pivots rejected equal highs/lows on the left side (TradingView allows them; only right-side ties reject)."""
import json
import os

from conftest import FIX
from pine_reference import simulate
from tbot.config import TF_MS, Params
from tbot.htf import CloseEmaSeries, htf_dir
from tbot.strategy import PairStrategy

SIREN_T, WOO_T = 1794680400000, 1794680700000  # bar open times (ms)


def load(sym):
    d = json.load(open(os.path.join(FIX, f"{sym}_5m_4h_20261115.json")))
    return [tuple(b) for b in d["5m"]], [tuple(b) for b in d["4h"]]


def run(sym):
    b5, b4 = load(sym)
    hist = CloseEmaSeries(50)
    for b in b4:
        hist.add(b[0], b[4])
    st = PairStrategy(Params(), sym, "5m")
    out, htf = {}, []
    for b in b5:
        hd = htf_dir(hist, TF_MS["4h"], b[0], TF_MS["5m"], b[4])
        htf.append(hd)
        sig, opened, _ = st.on_bar(b, hd)
        out[b[0]] = (sig, opened, st.engine.bear.armed, st.engine.bear.broken)
    return b5, out, htf


def test_siren_break_without_entry():
    _, out, htf = run("SIRENUSDT")
    sig, opened, armed, broken = out[SIREN_T]
    assert sig is None and opened is None
    assert armed and broken  # the break happened ('Gãy HT'), only the HTF filter blocks the SHORT


def test_woo_short_entry():
    b5, out, _ = run("WOOUSDT")
    sig, opened, _, _ = out[WOO_T]
    assert opened is not None and opened.side == "SHORT"
    assert opened.entry == 0.00933 == [b for b in b5 if b[0] == WOO_T][0][4]


def test_live_cases_match_pine_reference():
    for sym in ("SIRENUSDT", "WOOUSDT"):
        b5, out, htf = run(sym)
        ref_sigs, _ = simulate(b5, Params(), htf)
        mine = [(t, o[1].side) for t, o in out.items() if o[1] is not None]
        assert mine == ref_sigs
