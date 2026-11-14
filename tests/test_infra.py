import asyncio

import pytest

from tbot.htf import CloseEmaSeries, htf_dir
from tbot.ratelimit import WeightLimiter, klines_weight
from tbot.universe import filter_universe


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_klines_weight_table():
    assert [klines_weight(x) for x in (99, 100, 499, 500, 1000, 1500)] == [1, 2, 2, 5, 5, 10]


def test_limiter_window_budget():
    c = Clock()
    L = WeightLimiter(600, clock=c)
    for _ in range(300):
        assert L.wait_time(2) == 0
        L.record(2)
    assert L.used() == 600
    assert L.wait_time(1) > 59  # must wait for the window to slide
    c.t += 61
    assert L.wait_time(40) == 0 and L.peak_window == 600


def test_limiter_backoff_429_418_retry_after():
    c = Clock()
    L = WeightLimiter(600, clock=c)
    assert L.on_response(429, {"Retry-After": "120"}) == 120
    assert L.wait_time(1) == pytest.approx(120)
    c.t += 121
    assert L.wait_time(1) == 0
    d = L.on_response(418, {})
    assert d >= 300 and L.n_418 == 1
    L2 = WeightLimiter(600, clock=c)
    L2.on_response(429, {"Retry-After": "5"})  # floor 60 s
    assert L2.wait_time(1) >= 60


def test_limiter_server_weight_soft_cap():
    c = Clock()
    L = WeightLimiter(600, server_soft_cap=1200, clock=c)
    L.on_response(200, {"X-MBX-USED-WEIGHT-1M": "1300"})
    assert L.wait_time(1) > 0 and L.peak_server == 1300


def test_limiter_acquire_async():
    L = WeightLimiter(10)

    async def go():
        for _ in range(5):
            await L.acquire(2)
        return L.used()

    assert asyncio.run(go()) == 10


def test_universe_filter():
    ei = {"symbols": [
        {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"},
        {"symbol": "LOWUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"},
        {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "status": "SETTLING", "quoteAsset": "USDT"},
        {"symbol": "BTCUSDT_261225", "contractType": "CURRENT_QUARTER", "status": "TRADING", "quoteAsset": "USDT"},
        {"symbol": "ETHUSDC", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDC"},
        {"symbol": "EDGEUSDT", "contractType": "PERPETUAL", "status": "TRADING", "quoteAsset": "USDT"},
    ]}
    tk = [{"symbol": "BTCUSDT", "quoteVolume": "5e9"}, {"symbol": "LOWUSDT", "quoteVolume": "999999.9"},
          {"symbol": "OLDUSDT", "quoteVolume": "9e9"}, {"symbol": "BTCUSDT_261225", "quoteVolume": "9e9"},
          {"symbol": "ETHUSDC", "quoteVolume": "9e9"}, {"symbol": "EDGEUSDT", "quoteVolume": "1000000"}]
    assert [s for s, _ in filter_universe(ei, tk, 1_000_000)] == ["BTCUSDT", "EDGEUSDT"]


H, M, D = 14_400_000, 300_000, 86_400_000  # 4h HTF, 5m chart, 1d chart


def _series(closes):
    s = CloseEmaSeries(ema_len=2)
    for i, c in enumerate(closes):
        s.add(i * H, c)
    return s


def test_htf_4h_confirmed_bar_on_intraday_chart():
    """Pine request.security("240", lookahead_off), historical semantics: the last 4h bar whose close <= the chart
    bar close (never the forming 4h bar)."""
    s = _series([10, 10, 12, 8])  # ema(2): 10, 11.33, 9.11 ; bar 3 (8) closes at 4H
    # 5m bar 3H..3H+5m: last confirmed 4h bar is bar 2 (12 > ema) even if the chart close is far below
    assert htf_dir(s, H, 3 * H, M, chart_close=1.0) == 1
    # 5m bar closing at 4H closes together with 4h bar 3 (8 < ema) -> sees it
    assert htf_dir(s, H, 4 * H - M, M, chart_close=8.0) == -1
    assert htf_dir(s, H, 0, M) == 0  # no 4h bar closed yet


def test_htf_completes_bar_not_received_yet():
    s = _series([10, 10, 12])  # 4h bar 3 not received yet when the 5m bar closing at 4H arrives
    assert htf_dir(s, H, 4 * H - M, M, chart_close=8.0) == -1
    assert htf_dir(s, H, 4 * H - M, M, chart_close=20.0) == 1
    # not at a 4h boundary: no completion, previous confirmed bar
    assert htf_dir(s, H, 3 * H + M, M, chart_close=1.0) == 1


def test_htf_on_4h_and_1d_charts():
    s = _series([10] * 4 + [12, 8, 30])  # bar 5 (8) = last 4h bar of day 0 (closes at 1D); bar 6 is day 1
    # 4h chart = its own bar: bar 7 not yet in the series while it is being processed -> completed from its close
    s6 = _series([10] * 6 + [12])
    assert htf_dir(s6, H, 7 * H, H, chart_close=8.0) == -1
    assert htf_dir(s6, H, 7 * H, H, chart_close=13.0) == 1
    # 1d chart (lower-TF request): the last 4h bar of that day
    assert htf_dir(s, H, 0, D, chart_close=8.0) == -1


def test_close_ema_series_trims_and_matches_ema():
    s = CloseEmaSeries(ema_len=3, maxlen=10)
    for i in range(200):
        s.add(i * H, float(i))
    assert len(s.t) <= 110 and s.t[-1] == 199 * H and s.es[-1] == pytest.approx(198.0)
