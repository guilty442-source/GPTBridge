from __future__ import annotations

import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from .market_data_helpers import (
    _infer_frequency,
    _number,
    _yahoo_symbol_candidates,
    utc_now,
)


class MarketDataYahooMixin:
    """Yahoo Finance market data search."""

    def _search_yahoo(self, holding: dict[str, Any]) -> dict[str, Any]:
        symbol = str(holding.get("symbol") or "").strip().upper()
        market = str(holding.get("market") or "").strip().upper()
        candidates = _yahoo_symbol_candidates(symbol, market)
        if not candidates:
            return {"ok": False, "message": "缺少商品代碼"}
        requested = candidates[0]
        url = ""
        chart: dict[str, Any] = {}
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        for candidate in candidates:
            candidate_url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(candidate, safe="") + "?" + urllib.parse.urlencode({"range": "5y", "interval": "1d", "events": "div"})
            try:
                payload = self.fetch_json(candidate_url, None)
            except (OSError, urllib.error.URLError) as error:
                errors.append(f"{candidate}: {type(error).__name__}")
                continue
            candidate_chart = payload.get("chart") or {}
            candidate_results = candidate_chart.get("result") or []
            if candidate_results:
                requested = candidate
                url = candidate_url
                chart = candidate_chart
                results = candidate_results
                break
            error = candidate_chart.get("error") or {}
            errors.append(
                f"{candidate}: {str(error.get('description') or 'no result')}"
            )
        if not results:
            return {
                "ok": False,
                "message": "公開市場來源無報價：" + "; ".join(errors[:4]),
                "searched_symbols": candidates,
            }
        result = results[0]
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        valid = [(int(ts), _number(close, 0)) for ts, close in zip(timestamps, closes) if _number(close, 0) > 0]
        price = _number(meta.get("regularMarketPrice"), 0) or (valid[-1][1] if valid else 0)
        observed_timestamp = int(meta.get("regularMarketTime") or (valid[-1][0] if valid else 0))
        observed_at = datetime.fromtimestamp(observed_timestamp, timezone.utc).isoformat() if observed_timestamp else utc_now()
        # With a multi-year chart Yahoo's chartPreviousClose can refer to the
        # beginning of the requested range.  The last two valid daily bars are
        # the only safe inputs for a one-day change calculation.
        previous_close = valid[-2][1] if len(valid) >= 2 else 0
        events: list[dict[str, Any]] = []
        trailing_start = datetime.now(timezone.utc) - timedelta(days=366)
        for item in ((result.get("events") or {}).get("dividends") or {}).values():
            timestamp = int(item.get("date") or 0)
            amount = _number(item.get("amount"), -1)
            if timestamp <= 0 or amount < 0:
                continue
            occurred = datetime.fromtimestamp(timestamp, timezone.utc)
            events.append({"observed_at": occurred.isoformat(), "record_date": occurred.date().isoformat(), "amount_per_unit": amount, "currency": str(meta.get("currency") or holding.get("currency") or "").upper(), "source_name": "Yahoo Finance", "source_url": url})
        frequency = _infer_frequency(events)
        trailing = sum(_number(item.get("amount_per_unit")) for item in events if datetime.fromisoformat(str(item["observed_at"])) >= trailing_start)
        parameters = {
            "price": price,
            "previous_close": previous_close or None,
            "change_percent": ((price - previous_close) / previous_close * 100) if previous_close > 0 else None,
            "annual_distribution_per_unit": trailing,
            "distribution_yield_percent": (trailing / price * 100) if trailing > 0 and price > 0 else None,
            "distribution_frequency": frequency["code"],
        }
        return {
            "ok": price > 0,
            "identity_key": f"yahoo:{requested}",
            "requested_symbol": symbol,
            "requested_name": str(holding.get("name") or ""),
            "resolved_symbol": requested,
            "official_code": "",
            "isin": str(holding.get("isin") or ""),
            "name": str(meta.get("longName") or meta.get("shortName") or holding.get("name") or symbol),
            "market": market,
            "asset_type": str(holding.get("asset_type") or "AUTO").upper(),
            "currency": str(meta.get("currency") or holding.get("currency") or "").upper(),
            "quote_kind": "market_price",
            "observed_at": observed_at,
            "confidence": 0.78,
            "trusted": price > 0,
            "parameters": parameters,
            "parameter_units": {"price": "currency", "previous_close": "currency", "change_percent": "%", "annual_distribution_per_unit": "currency", "distribution_yield_percent": "%"},
            "distribution": {"frequency": frequency["code"], "frequency_label": frequency["label"], "frequency_per_year": frequency["per_year"], "frequency_confidence": frequency["confidence"], "trailing_annual_per_unit": trailing, "event_count": len(events)},
            "distribution_events": events,
            "sources": [{"name": "Yahoo Finance", "url": url, "kind": "public-market-chart", "observed_at": observed_at}],
            "searched_symbols": candidates,
        }
