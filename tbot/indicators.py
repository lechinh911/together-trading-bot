"""Incremental Pine-compatible indicators (ta.ema, ta.rma, ta.atr, ta.dmi, ta.sma, ta.rsi). NaN until seeded."""
from __future__ import annotations

import math
from collections import deque

NAN = float("nan")


def finite(x) -> bool:
    return x is not None and isinstance(x, (int, float)) and math.isfinite(x)


class EMA:
    """ta.ema: alpha=2/(n+1); first value = SMA of the first n inputs."""

    def __init__(self, n: int):
        self.n, self.alpha = n, 2.0 / (n + 1)
        self.acc, self.cnt, self.value = 0.0, 0, NAN

    def update(self, x: float) -> float:
        if not finite(x):
            return self.value
        if self.cnt < self.n:
            self.acc += x
            self.cnt += 1
            if self.cnt == self.n:
                self.value = self.acc / self.n
            return self.value
        self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class RMA(EMA):
    """ta.rma (Wilder): alpha=1/n, SMA seed."""

    def __init__(self, n: int):
        super().__init__(n)
        self.alpha = 1.0 / n


class SMA:
    def __init__(self, n: int):
        self.n, self.q, self.s = n, deque(), 0.0

    def update(self, x: float) -> float:
        self.q.append(x)
        self.s += x
        if len(self.q) > self.n:
            self.s -= self.q.popleft()
        return self.s / self.n if len(self.q) == self.n else NAN


class ATR:
    """ta.atr: rma(true range); first bar TR = high-low."""

    def __init__(self, n: int):
        self.rma = RMA(n)
        self.prev_close = None
        self.value = NAN

    @staticmethod
    def tr(h, l, pc):
        if pc is None:
            return h - l
        return max(h - l, abs(h - pc), abs(l - pc))

    def update(self, h, l, c) -> float:
        self.value = self.rma.update(self.tr(h, l, self.prev_close))
        self.prev_close = c
        return self.value


class DMI:
    """ta.dmi(diLen, adxSmoothing) -> (+DI, -DI, ADX). Bar 0 has na change -> skipped like Pine."""

    def __init__(self, di_len: int, adx_len: int):
        self.tr, self.p, self.m = RMA(di_len), RMA(di_len), RMA(di_len)
        self.adx = RMA(adx_len)
        self.prev = None  # (h, l, c)
        self.value = (NAN, NAN, NAN)

    def update(self, h, l, c):
        if self.prev is None:
            self.prev = (h, l, c)
            return self.value
        ph, pl, pc = self.prev
        up, dn = h - ph, pl - l
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        trur = self.tr.update(ATR.tr(h, l, pc))
        sp, sm = self.p.update(pdm), self.m.update(mdm)
        self.prev = (h, l, c)
        if not finite(trur) or trur == 0:
            return self.value
        plus, minus = 100.0 * sp / trur, 100.0 * sm / trur
        denom = plus + minus
        dx = 0.0 if denom == 0 else 100.0 * abs(plus - minus) / denom
        self.value = (plus, minus, self.adx.update(dx))
        return self.value


class RSI:
    def __init__(self, n: int):
        self.up, self.dn, self.prev, self.value = RMA(n), RMA(n), None, NAN

    def update(self, c):
        if self.prev is None:
            self.prev = c
            return self.value
        ch = c - self.prev
        self.prev = c
        u, d = self.up.update(max(ch, 0.0)), self.dn.update(max(-ch, 0.0))
        if finite(u) and finite(d):
            self.value = 100.0 if d == 0 else (0.0 if u == 0 else 100.0 - 100.0 / (1.0 + u / d))
        return self.value
