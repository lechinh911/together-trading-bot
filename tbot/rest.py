"""Minimal Binance USDT-M public REST client (no API key). Every call passes through the WeightLimiter."""
from __future__ import annotations

import asyncio
import logging

import aiohttp

from .ratelimit import WeightLimiter, klines_weight

log = logging.getLogger("tbot.rest")


class Rest:
    def __init__(self, base: str, limiter: WeightLimiter, session: aiohttp.ClientSession):
        self.base, self.lim, self.s = base, limiter, session
        self.startup_calls = None

    async def get(self, path: str, params: dict, weight: int, retries: int = 4):
        for attempt in range(retries):
            await self.lim.acquire(weight)
            try:
                async with self.s.get(self.base + path, params=params, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    backoff = self.lim.on_response(r.status, r.headers)
                    if r.status == 200:
                        return await r.json()
                    body = (await r.text())[:200]
                    log.warning("REST %s %s -> %s %s (backoff %ss)", path, params.get("symbol", ""), r.status, body, backoff)
                    if r.status in (429, 418):
                        continue  # limiter is now blocked until Retry-After
                    if 400 <= r.status < 500:
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("REST %s error %r", path, e)
            await asyncio.sleep(2 * (attempt + 1))
        return None

    async def exchange_info(self):
        return await self.get("/fapi/v1/exchangeInfo", {}, 1)

    async def tickers_24h(self):
        return await self.get("/fapi/v1/ticker/24hr", {}, 40)

    async def klines(self, symbol: str, interval: str, limit: int = 499, start_time: int | None = None):
        p = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_time is not None:
            p["startTime"] = start_time
        data = await self.get("/fapi/v1/klines", p, klines_weight(limit))
        if not data:
            return []
        return [(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), int(k[6])) for k in data]
