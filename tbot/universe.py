"""Universe: USDT-margined PERPETUAL contracts with status TRADING and 24h quote volume >= threshold."""
from __future__ import annotations


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
        if qv >= min_quote_volume:
            out.append((sym, qv))
    out.sort(key=lambda x: -x[1])
    return out
