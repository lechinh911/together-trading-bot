"""Higher-timeframe bias lookup: last CLOSED HTF bar whose close time <= the chart bar's open time (non-repainting,
equivalent to request.security(..., close[1], lookahead_on))."""
from __future__ import annotations

from bisect import bisect_right

from .indicators import EMA, finite


class SimpleSeries:
    """Close + EMA series for a TF without its own engine (1w)."""

    def __init__(self, ema_len=50):
        self.t, self.c, self.es = [], [], []
        self._ema = EMA(ema_len)

    def add(self, t, c):
        if self.t and t <= self.t[-1]:
            return
        self.t.append(t)
        self.c.append(c)
        self.es.append(self._ema.update(c))


def htf_dir(series, htf_ms: int, chart_open_t: int):
    """series has .t (open times), .c and .es lists. Returns +1 / -1 / 0 (unknown)."""
    if series is None or not series.t:
        return 0
    idx = bisect_right(series.t, chart_open_t - htf_ms) - 1
    if idx < 0:
        return 0
    c, e = series.c[idx], series.es[idx]
    if not (finite(c) and finite(e)):
        return 0
    return 1 if c > e else (-1 if c < e else 0)
