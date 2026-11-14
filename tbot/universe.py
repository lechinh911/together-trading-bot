"""Universe: USDT-margined PERPETUAL contracts with status TRADING and 24h quote volume >= threshold.

Excluded (not in Pine, which runs on one chart): stablecoin / pegged pairs whose price is ~1 USDT (risk of a pattern
is a tick or two, so fees dominate R), and any symbol whose 24h high-low range is < MIN_RANGE_PCT of its price."""
from __future__ import annotations

STABLE_BASES = frozenset({
    "USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "USDE", "PYUSD", "RLUSD", "USD1", "USDS", "USDD", "FRAX", "GUSD",
    "LUSD", "SUSD", "EURI", "AEUR", "EURC", "XUSD", "BFUSD", "USDF", "USDB", "USDX", "USDT0", "USDG",
})
MIN_RANGE_PCT = 0.3  # 24h (high - low) / last price, in %


def is_pegged(base: str, ticker: dict) -> bool:
    if base.upper() in STABLE_BASES:
        return True
    try:
        hi, lo, last = float(ticker["highPrice"]), float(ticker["lowPrice"]), float(ticker["lastPrice"])
    except (KeyError, TypeError, ValueError):
        return False
    return last > 0 and (hi - lo) / last * 100.0 < MIN_RANGE_PCT


def filter_universe(exchange_info: dict, tickers: list, min_quote_volume: float = 1_000_000.0) -> list:
    ok = {}
    for s in exchange_info.get("symbols", []):
        if s.get("contractType") == "PERPETUAL" and s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT":
            ok[s["symbol"]] = s
    out = []
    for t in tickers:
        sym = t.get("symbol")
        if sym not in ok:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
        except (TypeError, ValueError):
            continue
        if qv >= min_quote_volume and not is_pegged(ok[sym].get("baseAsset") or sym[:-4], t):
            out.append((sym, qv))
    out.sort(key=lambda x: -x[1])
    return out
