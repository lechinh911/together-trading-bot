"""REST weight limiter: sliding 60 s window kept well below Binance's 2400/min IP limit, plus global back-off on
HTTP 429 / 418 honouring Retry-After, and a pause when the server-reported used weight gets high."""
from __future__ import annotations

import asyncio
import time
from collections import deque


class WeightLimiter:
    def __init__(self, budget_per_min: int = 600, server_soft_cap: int = 1200, clock=time.monotonic, state_file=None):
        self.budget = budget_per_min
        self.server_soft_cap = server_soft_cap
        self.clock = clock
        self.window: deque = deque()  # (ts, weight)
        self.blocked_until = 0.0
        self.total_weight = 0
        self.total_calls = 0
        self.peak_window = 0  # max of our own 60 s weight sum
        self.peak_server = 0  # max X-MBX-USED-WEIGHT-1M seen
        self.last_server = 0
        self.n_429 = 0
        self.n_418 = 0
        self.lock = asyncio.Lock()
        self.state_file = state_file
        self._load_ban()

    def _load_ban(self):
        """A ban/back-off survives restarts: persisted as wall-clock time."""
        if not self.state_file:
            return
        try:
            import json
            until = float(json.load(open(self.state_file)).get("blocked_until_wall", 0))
            left = until - time.time()
            if left > 0:
                self.blocked_until = self.clock() + left
        except (OSError, ValueError):
            pass

    def _save_ban(self, delay):
        if not self.state_file:
            return
        import json
        try:
            with open(self.state_file, "w") as f:
                json.dump({"blocked_until_wall": time.time() + delay}, f)
        except OSError:
            pass

    def used(self, now=None) -> int:
        now = self.clock() if now is None else now
        while self.window and now - self.window[0][0] >= 60:
            self.window.popleft()
        return sum(w for _, w in self.window)

    def wait_time(self, weight: int, now=None) -> float:
        """Seconds to wait before `weight` can be spent (0 = now)."""
        now = self.clock() if now is None else now
        if now < self.blocked_until:
            return self.blocked_until - now
        used = self.used(now)
        if used + weight <= self.budget:
            return 0.0
        # wait until enough old entries expire
        need = used + weight - self.budget
        freed = 0
        for ts, w in self.window:
            freed += w
            if freed >= need:
                return max(0.0, ts + 60 - now) + 0.01
        return 60.0

    def record(self, weight: int, now=None):
        now = self.clock() if now is None else now
        self.window.append((now, weight))
        self.total_weight += weight
        self.total_calls += 1
        self.peak_window = max(self.peak_window, self.used(now))

    async def acquire(self, weight: int):
        async with self.lock:
            warned = False
            while True:
                w = self.wait_time(weight)
                if w > 30 and not warned:
                    warned = True
                    import logging
                    logging.getLogger("tbot.rest").warning("REST paused %.0fs (budget/back-off/ban)", w)
                if w <= 0:
                    self.record(weight)
                    return
                await asyncio.sleep(min(w, 5.0))

    def on_response(self, status: int, headers, now=None):
        """Feed the HTTP status + headers. Returns back-off seconds applied (0 if none)."""
        now = self.clock() if now is None else now
        used = headers.get("X-MBX-USED-WEIGHT-1M") or headers.get("x-mbx-used-weight-1m")
        if used is not None:
            try:
                self.last_server = int(used)
                self.peak_server = max(self.peak_server, self.last_server)
                if self.last_server >= self.server_soft_cap:
                    # pause until the next minute boundary of the server window
                    self.blocked_until = max(self.blocked_until, now + 60 - (time.time() % 60) + 1)
            except ValueError:
                pass
        if status in (429, 418):
            if status == 429:
                self.n_429 += 1
            else:
                self.n_418 += 1
            ra = headers.get("Retry-After") or headers.get("retry-after")
            try:
                delay = float(ra)
            except (TypeError, ValueError):
                delay = 120.0 if status == 429 else 600.0
            delay = max(delay, 60.0 if status == 429 else 300.0)
            self.blocked_until = max(self.blocked_until, now + delay)
            self._save_ban(delay)
            return delay
        return 0.0

    def stats(self) -> dict:
        return dict(calls=self.total_calls, weight_total=self.total_weight, weight_last_60s=self.used(),
                    weight_peak_60s=self.peak_window, server_weight_last=self.last_server,
                    server_weight_peak=self.peak_server, budget_per_min=self.budget,
                    http_429=self.n_429, http_418=self.n_418,
                    blocked_for_s=max(0, round(self.blocked_until - self.clock(), 1)),
                    blocked_until_t=(time.time() + self.blocked_until - self.clock()) if self.blocked_until > self.clock() else None)


def klines_weight(limit: int) -> int:
    """Binance USDT-M GET /fapi/v1/klines weight by `limit`."""
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10
