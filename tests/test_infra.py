import asyncio

import pytest

from tbot.htf import SimpleSeries, htf_dir
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


def test_htf_uses_last_closed_bar_only():
    """Pine lookahead_off (confirmed bars): the HTF bar whose close <= the chart bar close."""
    s = SimpleSeries(ema_len=2)
    H, M = 3_600_000, 300_000
    for i, c in enumerate([10, 10, 12, 8]):
        s.add(i * H, c)
    # 5m chart bar 3h00-3h05: last HTF bar closed by 3h05 is the 2h bar (close 12 > ema 11.33)
    assert htf_dir(s, H, 3 * H, M) == 1
    # chart bar 3h55-4h00 closes together with the 3h HTF bar (close 8 < ema) -> sees it
    assert htf_dir(s, H, 4 * H - M, M) == -1
    assert htf_dir(s, H, 0, M) == 0
    # HTF bar not received yet at the shared close instant: completed from the chart close + one EMA step
    s2 = SimpleSeries(ema_len=2)
    for i, c in enumerate([10, 10, 12]):
        s2.add(i * H, c)
    assert htf_dir(s2, H, 4 * H - M, M, chart_close=8.0, alpha=s2.ema_alpha) == -1
    assert htf_dir(s2, H, 4 * H - M, M, chart_close=20.0, alpha=s2.ema_alpha) == 1
