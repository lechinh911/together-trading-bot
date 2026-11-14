
import pytest

from tbot.config import Params
from tbot.trades import apply_bar, open_trade, pnl

P = Params()


def sig(side="LONG", entry=100.0, sl=95.0, atr=2.0):
    return dict(symbol="X", tf="1h", side=side, t=0, entry=entry, atr=atr, pattern_sl=sl, quality=0.5, neck=99.0)


def test_stop_cap_and_targets_long():
    tr = open_trade(sig(sl=90.0, atr=2.0), P)  # raw stop 10 away, cap 3 ATR = 6
    assert tr.stop == pytest.approx(94.0) and tr.risk == pytest.approx(6.0) and tr.tp == pytest.approx(109.0)
    tr = open_trade(sig(sl=97.0), P)
    assert tr.stop == 97.0 and tr.tp == pytest.approx(104.5)


def test_short_mirror_and_invalid_risk():
    tr = open_trade(sig("SHORT", entry=100, sl=104, atr=2), P)
    assert tr.stop == 104 and tr.tp == pytest.approx(94.0)
    assert open_trade(sig("LONG", entry=100, sl=100), P) is None


def test_same_bar_tradingview_path():
    """Both levels inside one bar: TradingView path, open nearer the low -> O-L-H-C (stop first) ..."""
    tr = open_trade(sig(sl=97.0), P)  # tp 104.5
    closed = apply_bar(tr, (1, 100, 105, 96, 101, 1), 2.0, P)
    assert closed and tr.exit_reason == "sl" and tr.exit_price == 97.0
    assert tr.r == pytest.approx(pnl(tr, 97.0, P)[0]) and tr.r < -1
    # ... open nearer the high -> O-H-L-C (TP first)
    tr = open_trade(sig(sl=97.0), P)
    assert apply_bar(tr, (1, 101, 105, 96, 101, 1), 2.0, P)
    assert tr.exit_reason == "tp" and tr.exit_price == 104.5


def test_gap_beyond_tp_fills_at_open():
    tr = open_trade(sig(sl=97.0), P)  # tp 104.5
    assert apply_bar(tr, (1, 106, 107, 96, 100, 1), 2.0, P)
    assert tr.exit_reason == "tp" and tr.exit_price == 106


def test_take_profit_and_commission():
    tr = open_trade(sig(sl=97.0), P)
    assert apply_bar(tr, (1, 100, 104.6, 99, 104, 1), 2.0, P)
    assert tr.exit_reason == "tp" and tr.exit_price == 104.5
    gross = 4.5
    comm = 0.0004 * (100 + 104.5)
    assert tr.r == pytest.approx((gross - comm) / 3.0)
    assert tr.pct == pytest.approx((gross - comm) / 100 * 100)


def test_breakeven_then_trail():
    tr = open_trade(sig(sl=97.0), P)  # risk 3 -> 1R at 103
    assert not apply_bar(tr, (1, 100, 103.2, 100.5, 103.0, 1), 2.0, P)
    assert tr.be and tr.stop >= 100.0
    assert tr.trail == pytest.approx(103.0 - 1.3 * 2.0)
    assert tr.stop == pytest.approx(max(100.0, 103.0 - 2.6))
    # trail never moves down
    assert not apply_bar(tr, (2, 103, 103.5, 102, 102.0, 1), 2.0, P)
    assert tr.trail == pytest.approx(103.0 - 2.6)
    # stop hit at breakeven/trail
    assert apply_bar(tr, (3, 102, 102.5, 99.0, 99.5, 1), 2.0, P)
    assert tr.exit_reason in ("be", "trail") and tr.exit_price == pytest.approx(100.4)


def test_gap_fill_at_open_and_no_reprocessing():
    tr = open_trade(sig(sl=97.0), P)
    assert apply_bar(tr, (1, 95, 96, 94, 95.5, 1), 2.0, P)
    assert tr.exit_price == 95  # gapped through the stop -> open
    tr2 = open_trade(sig(sl=97.0), P)
    assert not apply_bar(tr2, (0, 100, 200, 0, 100, 1), 2.0, P)  # bar not after entry: ignored


def test_short_same_bar_path():
    tr = open_trade(sig("SHORT", entry=100, sl=103, atr=2), P)  # risk 3, tp 95.5
    assert apply_bar(tr, (1, 100, 103.5, 95, 99, 1), 2.0, P)  # open nearer the high -> stop (above) first
    assert tr.exit_reason == "sl" and tr.exit_price == 103
    tr = open_trade(sig("SHORT", entry=100, sl=103, atr=2), P)
    assert apply_bar(tr, (1, 99, 103.5, 95, 99, 1), 2.0, P)  # open nearer the low -> TP (below) first
    assert tr.exit_reason == "tp" and tr.exit_price == 95.5
