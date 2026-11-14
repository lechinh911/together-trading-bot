"""Higher-timeframe (HTF) bias = Pine
    htfEma   = request.security(tickerid, "240", ta.ema(close, 50), gaps_off, lookahead_off)
    htfClose = request.security(tickerid, "240", close,             gaps_off, lookahead_off)
    htfBull = htfClose > htfEma ; htfBear = htfClose < htfEma
for EVERY chart timeframe (Pine default htfTf = "240"), with TradingView's HISTORICAL-bar semantics: on a chart bar
the value of the last 4h bar whose close time <= the chart bar's close time (a confirmed 4h bar, never the forming
one). This is what a TradingView chart shows after the script is (re)calculated, and it never repaints.
  - 5m/15m/1h chart: the previous confirmed 4h bar, except on the chart bar that closes together with a 4h bar,
    which already sees that 4h bar.
  - 4h chart: its own bar (close vs EMA50 of the 4h series).
  - 1d chart (lower-TF request): the last 4h bar of that day (it closes at the daily close).
If the 4h bar that closes at the same instant as the chart bar has not been received yet, it is completed from the
chart close (identical last price) and the EMA advanced one step, so the result does not depend on message order.
"""
from __future__ import annotations

from bisect import bisect_right

from .indicators import EMA, finite


class CloseEmaSeries:
    """(open_time, close, EMA(close)) history of the HTF timeframe, longer than the engine's own rolling window so
    that 1d warm-up replays still find their 4h values. Fed by the 4h Engine on every closed bar."""

    def __init__(self, ema_len: int = 50, maxlen: int = 700):
        self.t, self.c, self.es = [], [], []
        self._ema = EMA(ema_len)
        self.maxlen = maxlen

    @property
    def ema_alpha(self):
        return self._ema.alpha

    def add(self, t, c):
        if self.t and t <= self.t[-1]:
            return
        self.t.append(t)
        self.c.append(c)
        self.es.append(self._ema.update(c))
        if len(self.t) > self.maxlen + 100:
            del self.t[:100], self.c[:100], self.es[:100]


def htf_dir(series, htf_ms: int, chart_open_t: int, chart_ms: int, chart_close: float | None = None) -> int:
    """+1 htfBull / -1 htfBear / 0 unknown (na, equal, or no HTF data) for the chart bar [open, open+chart_ms).
    series: CloseEmaSeries of the HTF (bars aligned to multiples of htf_ms since the epoch, true for 4h)."""
    if series is None or not series.t:
        return 0
    chart_close_t = chart_open_t + chart_ms
    idx = bisect_right(series.t, chart_close_t - htf_ms) - 1  # last HTF bar with close <= chart close
    if idx < 0:
        return 0
    c, e = series.c[idx], series.es[idx]
    want_open = chart_close_t - htf_ms  # the HTF bar closing exactly with the chart bar (if aligned)
    if (chart_close is not None and chart_close_t % htf_ms == 0 and idx == len(series.t) - 1
            and series.t[idx] + htf_ms == want_open and finite(e)):
        a = series.ema_alpha  # that HTF bar is not in the series yet: complete it from the chart close
        c = chart_close
        e = a * c + (1 - a) * e
    if not (finite(c) and finite(e)):
        return 0
    return 1 if c > e else (-1 if c < e else 0)
