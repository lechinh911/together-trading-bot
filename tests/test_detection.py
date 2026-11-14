"""Detection on real Binance fixtures: the engine reproduces the independent v2 audit port bar for bar, and the
Active (v4) preset only adds filters on top of it."""
import json
import os
from dataclasses import replace

import pytest

from conftest import FIX, load_fixture
from tbot.config import Params
from tbot.engine import Engine, quality

V2 = replace(Params(), min_retrace=0.5, min_height_atr=1.5, opp_cancel=False, min_quality=0.0, break_buf=0.2,
             cancel_retest=True, use_volume=False, use_htf=False)
GOLD = json.load(open(os.path.join(FIX, "golden_v2_audit.json")))


def run(bars, p, htf=0):
    e = Engine(p, "X", "tf")
    out = []
    for b in bars:
        s = e.process_bar(b, htf)
        if s:
            out.append(s)
    return out


@pytest.mark.parametrize("name", ["BTCUSDT_4h", "ETHUSDT_1h"])
def test_matches_v2_audit_port(name):
    sigs = run(load_fixture(name + ".json"), V2)
    assert [[s["t"], s["side"]] for s in sigs] == GOLD[name]["signals"]


@pytest.mark.parametrize("name", ["BTCUSDT_4h", "ETHUSDT_1h"])
def test_active_preset_signals_are_consistent(name):
    bars = load_fixture(name + ".json")
    by_t = {b[0]: b for b in bars}
    p = replace(Params(), use_htf=False)
    sigs = run(bars, p)
    assert sigs, "Active preset should still find setups on 1500 real bars"
    for s in sigs:
        b = by_t[s["t"]]
        assert s["entry"] == b[4]  # entry at the signal bar close
        assert s["quality"] >= p.min_quality
        assert s["adx"] >= p.adx_min
        if s["side"] == "LONG":
            assert s["ema_fast"] > s["ema_slow"] and s["pattern_sl"] < s["entry"]
            assert s["ema_fast"] - s["ema_slow"] >= p.spread_atr * s["atr"]
        else:
            assert s["ema_fast"] < s["ema_slow"] and s["pattern_sl"] > s["entry"]
            assert s["ema_slow"] - s["ema_fast"] >= p.spread_atr * s["atr"]
        assert s["score"] >= p.min_score


def test_htf_bias_blocks_wrong_side():
    bars = load_fixture("BTCUSDT_4h.json")
    p = replace(Params(), use_htf=True)
    assert run(bars, p, htf=0) == []  # unknown HTF -> no signal
    longs = run(bars, p, htf=1)
    shorts = run(bars, p, htf=-1)
    assert all(s["side"] == "LONG" for s in longs)
    assert all(s["side"] == "SHORT" for s in shorts)


def test_no_repaint_prefix_invariance():
    """Signals up to bar k never change when more bars arrive (closed-candle processing)."""
    bars = load_fixture("ETHUSDT_1h.json")
    p = replace(Params(), use_htf=False)
    full = [(s["t"], s["side"]) for s in run(bars, p)]
    for k in (600, 900, 1200):
        part = [(s["t"], s["side"]) for s in run(bars[:k], p)]
        assert part == [x for x in full if x[0] <= bars[k - 1][0]]


def test_quality_formula():
    # perfect symmetry, mid spacing, tall pattern -> 1.0
    assert quality(100, 100, 2.5, 52.5, 1.0, 5, 100) == pytest.approx(1.0)
    # asymmetric by 1 ATR, spacing at the edge, height 0 -> 0
    assert quality(100, 101, 0.0, 5, 1.0, 5, 100) == pytest.approx(0.0, abs=1e-9)
    q = quality(100, 100.5, 1.25, 28.75, 1.0, 5, 100)
    assert q == pytest.approx(0.4 * 0.5 + 0.3 * 0.5 + 0.3 * 0.5)


def test_cooldown_and_flat_gate():
    bars = load_fixture("BTCUSDT_4h.json")
    p = replace(Params(), use_htf=False)
    assert run(bars, p)
    e = Engine(p, "X", "4h")
    assert not any(e.process_bar(b, 0, cool_ok=False) for b in bars)
    e = Engine(p, "X", "4h")
    assert not any(e.process_bar(b, 0, flat=False) for b in bars)
