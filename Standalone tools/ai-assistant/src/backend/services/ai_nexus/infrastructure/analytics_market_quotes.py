from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence

from .analytics_helpers import number, rounded, utc_text
from .analytics_market_common import FetchJson, _default_fetch_json, yahoo_symbol
from .analytics_market_yahoo import _fetch_all, _quote_field, _yahoo_chart_url

if TYPE_CHECKING:
    from .analytics_repository import InvestmentAnalyticsStore


def _intraday_bar(
    result: dict[str, Any],
    holding: dict[str, Any],
    symbol: str,
    market: str,
) -> dict[str, Any]:
    meta = result.get("meta") or {}
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    index = next(
        (
            item
            for item in range(min(len(timestamps), len(closes)) - 1, -1, -1)
            if number(closes[item], -1) > 0
        ),
        -1,
    )
    if index >= 0:
        timestamp = int(timestamps[index])
        close = number(closes[index])
        open_value = _quote_field(quote, "open", index)
        high_value = _quote_field(quote, "high", index)
        low_value = _quote_field(quote, "low", index)
        volume = _quote_field(quote, "volume", index)
    else:
        timestamp = int(meta.get("regularMarketTime") or 0)
        close = number(meta.get("regularMarketPrice"), 0)
        open_value = high_value = low_value = close
        volume = None
    if timestamp <= 0 or close <= 0:
        raise ValueError("報價來源沒有有效的盤中價格")
    return {
        "symbol": symbol,
        "observed_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
        "open": open_value,
        "high": high_value,
        "low": low_value,
        "close": close,
        "volume": volume,
        "currency": meta.get("currency") or holding.get("currency"),
        "provider": "yahoo-intraday",
        "verified": True,
    }


def _fetch_intraday_holding(fetch: FetchJson, holding: dict[str, Any]) -> dict[str, Any]:
    symbol = str(holding.get("symbol") or "").strip().upper()
    market = str(holding.get("market") or "").strip().upper()
    requested = yahoo_symbol(symbol, market)
    url = _yahoo_chart_url(requested, {"range": "1d", "interval": "1m"})
    payload = fetch(url)
    results = payload.get("chart", {}).get("result") or []
    if not results:
        raise ValueError("報價來源未回傳盤中資料")
    return {
        "symbol": symbol,
        "market": market,
        "requested_symbol": requested,
        "source_url": url,
        "bar": _intraday_bar(results[0], holding, symbol, market),
    }


def _quote_candidates(
    holdings: Sequence[dict[str, Any]],
    active_markets: set[str],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for holding in holdings:
        symbol = str(holding.get("symbol") or "").strip().upper()
        market = str(holding.get("market") or "").strip().upper()
        key = (market, symbol)
        if (
            symbol
            and market in active_markets
            and number(holding.get("quantity"), 0) > 0
            and key not in seen
        ):
            seen.add(key)
            candidates.append(holding)
    return candidates


def _quote_result(
    candidates: Sequence[dict[str, Any]],
    updates: list[dict[str, Any]],
    errors: list[dict[str, str]],
    prices_added: int,
    active_markets: set[str],
) -> dict[str, Any]:
    quotes = [
        {
            "symbol": item["symbol"],
            "market": item["market"],
            "current_price": item["bar"]["close"],
            "currency": item["bar"].get("currency"),
            "observed_at": item["bar"]["observed_at"],
            "source_url": item["source_url"],
        }
        for item in updates
        if isinstance(item.get("bar"), dict)
    ]
    return {
        "provider": "Yahoo Finance",
        "updated_at": utc_text(),
        "data_as_of": max(
            (str(item.get("observed_at") or "") for item in quotes),
            default="",
        ),
        "open_markets": sorted(active_markets),
        "requested_count": len(candidates),
        "updated_count": len(updates),
        "coverage_percent": (
            rounded(len(updates) / len(candidates) * 100, 2)
            if candidates
            else 100.0
        ),
        "prices_added": prices_added,
        "quotes": quotes,
        "error_count": len(errors),
        "errors": errors[:50],
        "methodology": "latest valid intraday close for exchanges currently in session",
        "limitations": [
            "Public endpoint availability and exchange delays may affect freshness.",
            "A quote is not a broker-executable price.",
        ],
    }


def sync_yahoo_open_market_quotes(
    store: InvestmentAnalyticsStore,
    holdings: Sequence[dict[str, Any]],
    open_markets: Sequence[str],
    *,
    fetch_json: FetchJson | None = None,
    max_workers: int = 12,
) -> dict[str, Any]:
    fetch = fetch_json or _default_fetch_json
    active_markets = {str(item or "").strip().upper() for item in open_markets}
    candidates = _quote_candidates(holdings, active_markets)
    updates, errors = _fetch_all(
        candidates,
        lambda holding: _fetch_intraday_holding(fetch, holding),
        max_workers,
    )
    bars = [item["bar"] for item in updates if isinstance(item.get("bar"), dict)]
    prices_added = store.add_price_bars(bars) if bars else 0
    return _quote_result(candidates, updates, errors, prices_added, active_markets)
