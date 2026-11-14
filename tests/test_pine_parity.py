"""Parity: tbot (incremental engine + PairStrategy) vs an independent full-array reference simulation of the official
v4 Pine strategy (tests/pine_reference.py) on recorded real Binance klines. Same signal bars, entries, stops, TPs,
exit bars and exit prices."""
from dataclasses import replace

import pytest

from conftest import load_fixture
from pine_reference import simulate
from tbot.config import Params
from tbot.strategy import PairStrategy


def run_tbot(bars, p, htf):
    st = PairStrategy(p, "X", "tf")
    sigs, trades = [], []
    for i, b in enumerate(bars):
        sig, opened, closed = st.on_bar(b, htf[i] if htf else 0)
        if closed:
            trades.append(dict(side=closed.side, entry_t=closed.entry_t, entry=closed.entry, stop0=closed.stop0,
                               tp=closed.tp, exit_t=closed.exit_t, exit=closed.exit_price))
        if opened:
            sigs.append((b[0], opened.side))
    return sigs, trades


def synthetic_htf(bars):
    """Deterministic HTF bias stand-in (alternating +1/-1/0 regimes of 150 bars) so the HTF gate really filters."""
    return [(1, -1, 0)[(i // 150) % 3] for i in range(len(bars))]


CASES = [(f, h) for f in ("BTCUSDT_4h.json", "ETHUSDT_1h.json") for h in (False, True)]


@pytest.mark.parametrize("fixture,use_htf", CASES)
def test_parity_with_pine_reference(fixture, use_htf):
    bars = load_fixture(fixture)
    p = replace(Params(), use_htf=use_htf)
    htf = synthetic_htf(bars) if use_htf else None
    ref_sigs, ref_tr = simulate(bars, p, htf)
    my_sigs, my_tr = run_tbot(bars, p, htf)
    assert ref_sigs, "fixture must produce signals"
    assert my_sigs == ref_sigs
    assert len(my_tr) == len(ref_tr)
    for a, b in zip(my_tr, ref_tr):
        assert a["side"] == b["side"] and a["entry_t"] == b["entry_t"] and a["exit_t"] == b["exit_t"]
        for k in ("entry", "stop0", "tp", "exit"):
            assert a[k] == pytest.approx(b[k], rel=1e-12), k
