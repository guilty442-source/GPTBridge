from __future__ import annotations

import statistics
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence

from .analytics_helpers import number, parse_datetime, rounded, utc_text
from .analytics_market_common import FetchJson, _default_fetch_json, yahoo_symbol
from .analytics_market_yahoo import _fetch_all, _yahoo_chart_url


def _infer_dividend_frequency(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
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


def _dividend_display_name(
    fetch: FetchJson,
    meta: dict[str, Any],
    holding: dict[str, Any],
    symbol: str,
    requested: str,
) -> str:
    display_name = str(meta.get("longName") or meta.get("shortName") or "").strip()
    existing_name = str(holding.get("name") or "").strip()
    if display_name or (existing_name and existing_name.upper() != symbol):
        return display_name
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
    return str(
        exact.get("longname") or exact.get("shortname") or ""
    ).strip()


def _dividend_events(
    result: dict[str, Any],
    trailing_start: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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
    return trailing_events, frequency_events


def _fetch_dividend_holding(
    holding: dict[str, Any],
    *,
    fetch: FetchJson,
    period: str,
    trailing_start: datetime,
) -> dict[str, Any]:
    symbol = str(holding.get("symbol") or "").strip().upper()
    market = str(holding.get("market") or "").strip().upper()
    requested = yahoo_symbol(symbol, market)
    url = _yahoo_chart_url(
        requested,
        {"range": period, "interval": "1mo", "events": "div"},
    )
    payload = fetch(url)
    results = payload.get("chart", {}).get("result") or []
    if not results:
        error = payload.get("chart", {}).get("error") or {}
        raise ValueError(str(error.get("description") or "No dividend result"))
    result = results[0]
    meta = result.get("meta") or {}
    display_name = _dividend_display_name(fetch, meta, holding, symbol, requested)
    trailing_events, frequency_events = _dividend_events(result, trailing_start)
    trailing_annual_per_unit = sum(
        number(item.get("amount_per_unit")) for item in trailing_events
    )
    frequency = _infer_dividend_frequency(frequency_events)
    current_price = number(
        meta.get("regularMarketPrice") or meta.get("chartPreviousClose"),
        0,
    )
    return _dividend_result(
        symbol,
        market,
        requested,
        url,
        meta,
        holding,
        display_name,
        trailing_events,
        trailing_annual_per_unit,
        current_price,
        frequency,
    )


def _dividend_result(
    symbol: str,
    market: str,
    requested: str,
    url: str,
    meta: dict[str, Any],
    holding: dict[str, Any],
    display_name: str,
    trailing_events: list[dict[str, Any]],
    trailing_annual_per_unit: float,
    current_price: float,
    frequency: dict[str, Any],
) -> dict[str, Any]:
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
    updates, errors = _fetch_all(
        candidates,
        lambda holding: _fetch_dividend_holding(
            holding,
            fetch=fetch,
            period=period,
            trailing_start=trailing_start,
        ),
        max_workers,
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
