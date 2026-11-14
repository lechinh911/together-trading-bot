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

    @property
    def ema_alpha(self):
        return self._ema.alpha


def htf_dir(series, htf_ms: int, chart_open_t: int, chart_ms: int = 0, chart_close: float | None = None, alpha=None):
    """Pine request.security(htf, ..., lookahead_off) on confirmed bars: the HTF bar whose close time <= the chart
    bar's close time (open + chart_ms). If that HTF bar closes at the same instant as the chart bar but has not been
    received yet, it is completed from the chart close (same last price) and the HTF EMA is advanced by one step.
    series has .t (open times), .c and .es lists. Returns +1 / -1 / 0 (unknown)."""
    if series is None or not series.t:
        return 0
    chart_close_t = chart_open_t + chart_ms
    idx = bisect_right(series.t, chart_close_t - htf_ms) - 1
    if idx < 0:
        return 0
    c, e = series.c[idx], series.es[idx]
    want_open = ((chart_close_t // htf_ms) * htf_ms) - htf_ms if chart_ms else None
    if (chart_ms and chart_close is not None and alpha and idx == len(series.t) - 1 and series.t[idx] < want_open
            and series.t[idx] + htf_ms == want_open and chart_close_t % htf_ms == 0 and finite(e)):
        c = chart_close
        e = alpha * c + (1 - alpha) * e
    if not (finite(c) and finite(e)):
        return 0
    return 1 if c > e else (-1 if c < e else 0)
