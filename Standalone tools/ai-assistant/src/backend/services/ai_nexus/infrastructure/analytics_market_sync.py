from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .privacy import decode_json_document, protect_text, unprotect_text
from .analytics_helpers import number, parse_datetime, utc_now, utc_text

FetchJson = Callable[[str], Any]

POSITIVE_WORDS = {"beat", "growth", "raise", "upgrade", "profit", "surge", "record", "成長", "上修", "獲利", "創高", "優於"}
NEGATIVE_WORDS = {"miss", "cut", "downgrade", "loss", "fall", "risk", "fraud", "下修", "虧損", "衰退", "風險", "裁員"}

def sentiment_score(text: str) -> tuple[float, float]:
    lowered = str(text or "").lower()
    positive = sum(1 for word in POSITIVE_WORDS if word in lowered)
    negative = sum(1 for word in NEGATIVE_WORDS if word in lowered)
    total = positive + negative
    return ((positive - negative) / total if total else 0.0, min(1.0, 0.35 + total * 0.12))


def fetch_json_with_retry(
    url: str,
    *,
    opener: Callable[..., Any] | None = None,
    sleep: Callable[[float], None] | None = None,
    attempts: int = 3,
    timeout_seconds: float = 15,
    base_delay_seconds: float = 0.25,
) -> Any:
    """Decode injected JSON data without granting this tool direct network access."""
    if opener is None:
        raise PermissionError(
            "AI investment manager has no network access; Xingcheng must inject market data."
        )
    open_url = opener
    wait = sleep or time.sleep
    maximum_attempts = max(1, min(5, int(attempts)))
    last_error: Exception | None = None
    for attempt in range(maximum_attempts):
        try:
            with open_url(url, timeout=max(1.0, min(30.0, timeout_seconds))) as response:
                status = int(getattr(response, "status", 200) or 200)
                if status == 429 or 500 <= status <= 599:
                    raise urllib.error.HTTPError(
                        url,
                        status,
                        f"transient HTTP status {status}",
                        getattr(response, "headers", None),
                        None,
                    )
                if status >= 400:
                    raise urllib.error.HTTPError(
                        url,
                        status,
                        f"HTTP status {status}",
                        getattr(response, "headers", None),
                        None,
                    )
                return json.loads(
                    response.read().decode("utf-8", errors="replace")
                )
        except urllib.error.HTTPError as exc:
            last_error = exc
            transient = exc.code == 429 or 500 <= exc.code <= 599
            if not transient or attempt + 1 >= maximum_attempts:
                raise
            retry_after = 0.0
            try:
                retry_after = float(exc.headers.get("Retry-After") or 0)
            except (AttributeError, TypeError, ValueError):
                retry_after = 0.0
            wait(
                min(
                    2.0,
                    max(
                        retry_after,
                        max(0.0, base_delay_seconds) * (2**attempt),
                    ),
                )
            )
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 >= maximum_attempts:
                raise
            wait(min(2.0, max(0.0, base_delay_seconds) * (2**attempt)))
    if last_error is not None:
        raise last_error
    raise RuntimeError("public JSON fetch failed")


def _default_fetch_json(url: str) -> Any:
    raise PermissionError(
        "AI investment manager has no network access; Xingcheng must provide market data."
    )


def yahoo_symbol(symbol: str, market: str) -> str:
    normalized = symbol.strip().upper()
    if market.upper() == "TW" and not normalized.endswith((".TW", ".TWO")):
        return f"{normalized}.TW"
    if market.upper() == "HK" and not normalized.endswith(".HK"):
        return f"{int(normalized):04d}.HK" if normalized.isdigit() else f"{normalized}.HK"
    if market.upper() == "CRYPTO" and "-" not in normalized:
        return f"{normalized}-USD"
    return normalized


MARKET_SESSION_DEFINITIONS: dict[str, dict[str, Any]] = {
    "TW": {
        "timezone": "Asia/Taipei",
        "sessions": ((9 * 60, 13 * 60 + 30),),
        "schedule": "09:00-13:30",
    },
    "US": {
        "timezone": "America/New_York",
        "sessions": ((9 * 60 + 30, 16 * 60),),
        "schedule": "09:30-16:00",
    },
    "HK": {
        "timezone": "Asia/Hong_Kong",
        "sessions": ((9 * 60 + 30, 12 * 60), (13 * 60, 16 * 60)),
        "schedule": "09:30-12:00 / 13:00-16:00",
    },
}


def market_session_status(now: datetime | None = None) -> dict[str, Any]:
    observed = now or utc_now()
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    observed = observed.astimezone(timezone.utc)
    markets: dict[str, dict[str, Any]] = {}
    open_markets: list[str] = []

    def nth_sunday(year: int, month: int, occurrence: int) -> int:
        first = datetime(year, month, 1, tzinfo=timezone.utc)
        return 1 + (6 - first.weekday()) % 7 + (occurrence - 1) * 7

    def local_market_time(timezone_name: str) -> datetime:
        if timezone_name in {"Asia/Taipei", "Asia/Hong_Kong"}:
            return observed.astimezone(timezone(timedelta(hours=8)))
        year = observed.year
        dst_start = datetime(
            year,
            3,
            nth_sunday(year, 3, 2),
            7,
            tzinfo=timezone.utc,
        )
        dst_end = datetime(
            year,
            11,
            nth_sunday(year, 11, 1),
            6,
            tzinfo=timezone.utc,
        )
        eastern_offset = -4 if dst_start <= observed < dst_end else -5
        return observed.astimezone(timezone(timedelta(hours=eastern_offset)))

    for market, definition in MARKET_SESSION_DEFINITIONS.items():
        local_time = local_market_time(str(definition["timezone"]))
        minute = local_time.hour * 60 + local_time.minute
        weekday = local_time.weekday() < 5
        is_open = weekday and any(
            start <= minute < end for start, end in definition["sessions"]
        )
        if is_open:
            open_markets.append(market)
        markets[market] = {
            "market": market,
            "is_open": is_open,
            "timezone": definition["timezone"],
            "schedule": definition["schedule"],
            "local_time": local_time.isoformat(),
            "weekday": weekday,
        }
    return {
        "as_of": utc_text(observed),
        "open_markets": open_markets,
        "markets": markets,
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

    def fetch_holding(holding: dict[str, Any]) -> dict[str, Any]:
        symbol = str(holding.get("symbol") or "").strip().upper()
        market = str(holding.get("market") or "").strip().upper()
        requested = yahoo_symbol(symbol, market)
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            + urllib.parse.quote(requested, safe="")
            + "?"
            + urllib.parse.urlencode({"range": "1d", "interval": "1m"})
        )
        payload = fetch(url)
        results = payload.get("chart", {}).get("result") or []
        if not results:
            raise ValueError("報價來源未回傳盤中資料")
        result = results[0]
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

            def value(field: str) -> Any:
                values = quote.get(field) or []
                return values[index] if index < len(values) else None

            open_value = value("open")
            high_value = value("high")
            low_value = value("low")
            volume = value("volume")
        else:
            timestamp = int(meta.get("regularMarketTime") or 0)
            close = number(meta.get("regularMarketPrice"), 0)
            open_value = high_value = low_value = close
            volume = None
        if timestamp <= 0 or close <= 0:
            raise ValueError("報價來源沒有有效的盤中價格")
        return {
            "symbol": symbol,
            "market": market,
            "requested_symbol": requested,
            "source_url": url,
            "bar": {
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
            },
        }

    updates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    workers = max(1, min(int(max_workers or 1), 12))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_holding, holding): holding for holding in candidates
        }
        for future in as_completed(futures):
            holding = futures[future]
            try:
                updates.append(future.result())
            except Exception as exc:
                errors.append(
                    {
                        "symbol": str(holding.get("symbol") or ""),
                        "market": str(holding.get("market") or ""),
                        "message": str(exc),
                    }
                )
    bars = [item["bar"] for item in updates if isinstance(item.get("bar"), dict)]
    prices_added = store.add_price_bars(bars) if bars else 0
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


def sync_yahoo_dividends(
    holdings: Sequence[dict[str, Any]],
    *,
    fetch_json: FetchJson | None = None,
    period: str = "2y",
    max_workers: int = 8,
) -> dict[str, Any]:
    fetch = fetch_json or _default_fetch_json
    candidates = [
        holding
        for holding in holdings
        if str(holding.get("market") or "").upper() in {"TW", "US", "HK"}
        and str(holding.get("symbol") or "").strip()
        and number(holding.get("quantity")) > 0
    ][:300]
    trailing_start = datetime.now(timezone.utc) - timedelta(days=366)

    def infer_frequency(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
        event_dates = sorted(
            {
                parsed
                for event in events
                if (parsed := parse_datetime(event.get("occurred_at"))) is not None
            }
        )
        if len(event_dates) < 2:
            return {
                "dividend_frequency": "unknown",
                "dividend_frequency_label": "待累積資料",
                "dividend_frequency_per_year": None,
                "dividend_frequency_confidence": 0.0,
            }
        intervals = [
            (event_dates[index] - event_dates[index - 1]).total_seconds() / 86400
            for index in range(1, len(event_dates))
        ]
        median_days = statistics.median(intervals)
        definitions = (
            (10, "weekly", "每週", 52),
            (20, "biweekly", "每兩週", 26),
            (45, "monthly", "每月", 12),
            (75, "bimonthly", "每兩月", 6),
            (120, "quarterly", "每季", 4),
            (220, "semiannual", "每半年", 2),
            (420, "annual", "每年", 1),
        )
        code, label, per_year = "irregular", "不定期", None
        for maximum_days, candidate_code, candidate_label, candidate_per_year in definitions:
            if median_days <= maximum_days:
                code, label, per_year = (
                    candidate_code,
                    candidate_label,
                    candidate_per_year,
                )
                break
        tolerance = max(7.0, median_days * 0.45)
        if len(intervals) >= 3 and max(abs(value - median_days) for value in intervals) > tolerance:
            code, label, per_year = "irregular", "不定期", None
        confidence = min(0.98, 0.58 + len(intervals) * 0.1)
        return {
            "dividend_frequency": code,
            "dividend_frequency_label": label,
            "dividend_frequency_per_year": per_year,
            "dividend_frequency_median_days": rounded(median_days, 2),
            "dividend_frequency_confidence": rounded(confidence, 2),
        }

    def fetch_holding(holding: dict[str, Any]) -> dict[str, Any]:
        symbol = str(holding.get("symbol") or "").strip().upper()
        market = str(holding.get("market") or "").strip().upper()
        requested = yahoo_symbol(symbol, market)
        url = (
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            + urllib.parse.quote(requested, safe="")
            + "?"
            + urllib.parse.urlencode(
                {"range": period, "interval": "1mo", "events": "div"}
            )
        )
        payload = fetch(url)
        results = payload.get("chart", {}).get("result") or []
        if not results:
            error = payload.get("chart", {}).get("error") or {}
            raise ValueError(str(error.get("description") or "No dividend result"))
        result = results[0]
        meta = result.get("meta") or {}
        display_name = str(meta.get("longName") or meta.get("shortName") or "").strip()
        existing_name = str(holding.get("name") or "").strip()
        if not display_name and (not existing_name or existing_name.upper() == symbol):
            search_url = (
                "https://query1.finance.yahoo.com/v1/finance/search?"
                + urllib.parse.urlencode(
                    {"q": requested, "quotesCount": 5, "newsCount": 0}
                )
            )
            search_payload = fetch(search_url)
            quotes = search_payload.get("quotes") or []
            exact = next(
                (
                    quote
                    for quote in quotes
                    if str(quote.get("symbol") or "").upper() == requested.upper()
                ),
                quotes[0] if quotes else {},
            )
            display_name = str(
                exact.get("longname") or exact.get("shortname") or ""
            ).strip()
        events = (result.get("events") or {}).get("dividends") or {}
        trailing_events: list[dict[str, Any]] = []
        frequency_events: list[dict[str, Any]] = []
        for entry in events.values():
            timestamp = int(entry.get("date") or 0)
            amount = number(entry.get("amount"), -1)
            if timestamp <= 0 or amount < 0:
                continue
            occurred_at = datetime.fromtimestamp(timestamp, timezone.utc)
            frequency_events.append(
                {
                    "occurred_at": occurred_at.isoformat(),
                    "amount_per_unit": amount,
                }
            )
            if occurred_at >= trailing_start:
                trailing_events.append(
                    {
                        "occurred_at": occurred_at.isoformat(),
                        "amount_per_unit": amount,
                    }
                )
        trailing_annual_per_unit = sum(
            number(item.get("amount_per_unit")) for item in trailing_events
        )
        frequency = infer_frequency(frequency_events)
        current_price = number(
            meta.get("regularMarketPrice") or meta.get("chartPreviousClose"),
            0,
        )
        yield_percent = (
            trailing_annual_per_unit / current_price * 100.0
            if current_price > 0 and trailing_annual_per_unit > 0
            else None
        )
        return {
            "symbol": symbol,
            "market": market,
            "requested_symbol": requested,
            "currency": str(meta.get("currency") or holding.get("currency") or "").upper(),
            "name": display_name,
            "instrument_type": str(meta.get("instrumentType") or "").upper(),
            "exchange_name": str(
                meta.get("fullExchangeName") or meta.get("exchangeName") or ""
            ),
            "current_price": rounded(current_price, 6),
            "trailing_annual_dividend_per_unit": rounded(
                trailing_annual_per_unit, 6
            ),
            "annual_dividend_yield_percent": rounded(yield_percent, 4),
            "event_count": len(trailing_events),
            "events": trailing_events,
            **frequency,
            "source": "Yahoo Finance",
            "source_url": url,
            "updated_at": utc_text(),
            "status": "updated" if trailing_events else "no_external_dividend",
        }

    updates: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    workers = max(1, min(int(max_workers or 1), 12))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(fetch_holding, holding): holding for holding in candidates
        }
        for future in as_completed(futures):
            holding = futures[future]
            try:
                updates.append(future.result())
            except Exception as exc:
                errors.append(
                    {
                        "symbol": str(holding.get("symbol") or ""),
                        "message": str(exc),
                    }
                )
    updates.sort(key=lambda item: (str(item.get("market")), str(item.get("symbol"))))
    return {
        "requested_count": len(candidates),
        "updated_count": sum(item.get("status") == "updated" for item in updates),
        "no_dividend_count": sum(
            item.get("status") == "no_external_dividend" for item in updates
        ),
        "error_count": len(errors),
        "updates": updates,
        "errors": errors[:50],
        "provider": "Yahoo Finance",
        "updated_at": utc_text(),
        "coverage_percent": (
            rounded(len(updates) / len(candidates) * 100, 2)
            if candidates
            else 100.0
        ),
        "methodology": "trailing cash distributions from public chart events",
        "limitations": [
            "Distribution currency is not converted in this fetch step.",
            "Historical distributions do not guarantee future payments.",
        ],
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
            chart_url = (
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                + urllib.parse.quote(requested, safe="")
                + "?"
                + urllib.parse.urlencode({"range": period, "interval": "1d", "events": "div,splits"})
            )
            payload = fetch(chart_url)
            result = payload.get("chart", {}).get("result", [])[0]
            timestamps = result.get("timestamp") or []
            quote = (result.get("indicators", {}).get("quote") or [{}])[0]
            meta = result.get("meta") or {}
            bars = []
            def quote_value(field: str, index: int) -> Any:
                values = quote.get(field) or []
                return values[index] if index < len(values) else None

            for index, timestamp in enumerate(timestamps):
                close_values = quote.get("close") or []
                if index >= len(close_values) or number(close_values[index], -1) <= 0:
                    continue
                bars.append(
                    {
                        "symbol": symbol,
                        "observed_at": datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat(),
                        "open": quote_value("open", index),
                        "high": quote_value("high", index),
                        "low": quote_value("low", index),
                        "close": close_values[index],
                        "volume": quote_value("volume", index),
                        "currency": meta.get("currency") or holding.get("currency"),
                        "provider": "yahoo-history",
                        "verified": True,
                    }
                )
            prices_added += store.add_price_bars(bars)
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
        except Exception as exc:
            errors.append({"symbol": symbol, "phase": "history", "message": str(exc)})
        try:
            search_url = "https://query1.finance.yahoo.com/v1/finance/search?" + urllib.parse.urlencode({"q": requested, "newsCount": 8, "quotesCount": 0})
            payload = fetch(search_url)
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
        except Exception as exc:
            errors.append({"symbol": symbol, "phase": "news", "message": str(exc)})
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

__all__ = ['FetchJson', 'sentiment_score', 'fetch_json_with_retry', '_default_fetch_json', 'yahoo_symbol', 'market_session_status', 'sync_yahoo_open_market_quotes', 'sync_yahoo_dividends', 'sync_yahoo_intelligence']
