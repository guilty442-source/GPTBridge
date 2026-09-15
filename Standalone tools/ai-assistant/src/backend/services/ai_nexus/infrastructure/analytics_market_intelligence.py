from __future__ import annotations

import urllib.parse
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Sequence

from .analytics_helpers import number, rounded, utc_text
from .analytics_market_common import (
    FetchJson,
    _default_fetch_json,
    sentiment_score,
    yahoo_symbol,
)
from .analytics_market_yahoo import _quote_field, _yahoo_chart_url

if TYPE_CHECKING:
    from .analytics_repository import InvestmentAnalyticsStore


def _history_bars(
    symbol: str,
    holding: dict[str, Any],
    meta: dict[str, Any],
    quote: dict[str, Any],
    timestamps: Sequence[Any],
) -> list[dict[str, Any]]:
    bars = []
    close_values = quote.get("close") or []
    for index, timestamp in enumerate(timestamps):
        if index >= len(close_values) or number(close_values[index], -1) <= 0:
            continue
        bars.append(
            {
                "symbol": symbol,
                "observed_at": datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(),
                "open": _quote_field(quote, "open", index),
                "high": _quote_field(quote, "high", index),
                "low": _quote_field(quote, "low", index),
                "close": close_values[index],
                "volume": _quote_field(quote, "volume", index),
                "currency": meta.get("currency") or holding.get("currency"),
                "provider": "yahoo-history",
                "verified": True,
            }
        )
    return bars


def _store_chart_events(
    store: InvestmentAnalyticsStore,
    result: dict[str, Any],
    symbol: str,
) -> int:
    events_added = 0
    chart_events = result.get("events") or {}
    for event_type, entries in chart_events.items():
        for entry in (entries or {}).values():
            store.add_event(
                {
                    "event_type": "dividend" if event_type == "dividends" else "split",
                    "symbol": symbol,
                    "title": f"{symbol} {'股息' if event_type == 'dividends' else '拆股'}",
                    "scheduled_at": entry.get("date"),
                    "source": "Yahoo Finance",
                    "confidence": 0.85,
                    "details": entry,
                    "dedupe_key": f"yahoo|{symbol}|{event_type}|{entry.get('date')}",
                }
            )
            events_added += 1
    return events_added


def _sync_holding_history(
    store: InvestmentAnalyticsStore,
    fetch: FetchJson,
    symbol: str,
    holding: dict[str, Any],
    requested: str,
    period: str,
) -> tuple[int, int]:
    chart_url = _yahoo_chart_url(
        requested,
        {"range": period, "interval": "1d", "events": "div,splits"},
    )
    payload = fetch(chart_url)
    result = payload.get("chart", {}).get("result", [])[0]
    timestamps = result.get("timestamp") or []
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    meta = result.get("meta") or {}
    bars = _history_bars(symbol, holding, meta, quote, timestamps)
    prices_added = store.add_price_bars(bars)
    events_added = _store_chart_events(store, result, symbol)
    return prices_added, events_added


def _sync_holding_news(
    store: InvestmentAnalyticsStore,
    fetch: FetchJson,
    symbol: str,
    requested: str,
) -> int:
    search_url = "https://query1.finance.yahoo.com/v1/finance/search?" + urllib.parse.urlencode({"q": requested, "newsCount": 8, "quotesCount": 0})
    payload = fetch(search_url)
    news_added = 0
    for item in payload.get("news", [])[:8]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        score, confidence = sentiment_score(title)
        store.add_event(
            {
                "event_type": "news",
                "symbol": symbol,
                "title": title,
                "published_at": item.get("providerPublishTime"),
                "source": item.get("publisher") or "Yahoo Finance",
                "source_url": item.get("link") or "",
                "sentiment": score,
                "confidence": confidence,
                "status": "published",
                "dedupe_key": str(item.get("uuid") or item.get("link") or title),
                "details": {
                    "sentiment_methodology": "lexical_heuristic",
                    "supported_languages": ["English", "Traditional Chinese keyword subset"],
                    "limitations": [
                        "No sarcasm, negation, context or entity-level interpretation.",
                        "Confidence reflects keyword coverage, not predictive certainty.",
                    ],
                },
            }
        )
        news_added += 1
    return news_added


def _intelligence_result(
    prices_added: int,
    events_added: int,
    news_added: int,
    errors: list[dict[str, str]],
    holdings: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ok": not errors or prices_added > 0,
        "prices_added": prices_added,
        "events_added": events_added,
        "news_added": news_added,
        "errors": errors,
        "provider": "Yahoo Finance",
        "updated_at": utc_text(),
        "requested_count": min(50, len(holdings)),
        "coverage_percent": (
            rounded(
                (min(50, len(holdings)) - len({item.get("symbol") for item in errors}))
                / min(50, len(holdings))
                * 100,
                2,
            )
            if holdings[:50]
            else 100.0
        ),
        "sentiment_methodology": {
            "method": "lexical_heuristic",
            "is_ai_model": False,
            "languages": ["English", "Traditional Chinese keyword subset"],
            "limitations": "Keyword polarity only; do not use as a trading signal.",
        },
        "methodology": "public daily OHLCV, corporate events and headline keyword polarity",
        "message": f"市場情報同步完成：{prices_added} 筆行情、{events_added + news_added} 筆事件。",
    }


def sync_yahoo_intelligence(
    store: InvestmentAnalyticsStore,
    holdings: Sequence[dict[str, Any]],
    *,
    fetch_json: FetchJson | None = None,
    period: str = "1y",
) -> dict[str, Any]:
    fetch = fetch_json or _default_fetch_json
    prices_added = 0
    events_added = 0
    news_added = 0
    errors: list[dict[str, str]] = []
    for holding in holdings[:50]:
        symbol = str(holding.get("symbol") or "").strip().upper()
        market = str(holding.get("market") or "").strip().upper()
        requested = yahoo_symbol(symbol, market)
        if not symbol:
            continue
        try:
            added_prices, added_events = _sync_holding_history(
                store, fetch, symbol, holding, requested, period
            )
            prices_added += added_prices
            events_added += added_events
        except Exception as exc:
            errors.append({"symbol": symbol, "phase": "history", "message": str(exc)})
        try:
            news_added += _sync_holding_news(store, fetch, symbol, requested)
        except Exception as exc:
            errors.append({"symbol": symbol, "phase": "news", "message": str(exc)})
    return _intelligence_result(
        prices_added, events_added, news_added, errors, holdings
    )
