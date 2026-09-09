from __future__ import annotations

import copy
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Any

from .market_data_helpers import (
    TPEX_BASE,
    TWSE_BASE,
    _infer_frequency,
    _number,
    _roc_date_iso,
    utc_now,
)


class MarketDataTwOfficialMixin:
    """Taiwan official market data search (TWSE / TPEx)."""

    def _search_tw_official(self, holding: dict[str, Any]) -> dict[str, Any]:
        symbol = str(holding.get("symbol") or "").strip().upper()
        if not symbol:
            return {"ok": False, "message": "缺少臺灣商品代碼"}
        datasets = (
            (
                "TWSE",
                f"{TWSE_BASE}/exchangeReport/STOCK_DAY_ALL",
                "Code",
                "ClosingPrice",
                "Change",
                "Date",
                "臺灣證券交易所",
            ),
            (
                "TPEx",
                f"{TPEX_BASE}/tpex_mainboard_daily_close_quotes",
                "SecuritiesCompanyCode",
                "Close",
                "Change",
                "Date",
                "證券櫃檯買賣中心",
            ),
        )
        matched: dict[str, Any] | None = None
        source: dict[str, Any] | None = None
        change = 0.0
        observed_at = ""
        for exchange, url, code_key, price_key, change_key, date_key, source_name in datasets:
            try:
                row = next(
                    (
                        item
                        for item in self._cached_dataset(url, 300)
                        if str(item.get(code_key) or "").strip().upper() == symbol
                    ),
                    None,
                )
            except (OSError, urllib.error.URLError, ValueError):
                row = None
            if row is None:
                continue
            price = _number(row.get(price_key), 0)
            if price <= 0:
                continue
            matched = {"exchange": exchange, "price": price, "row": row}
            change = _number(str(row.get(change_key) or "").replace("X", ""), 0)
            observed_at = _roc_date_iso(row.get(date_key)) or utc_now()
            source = {
                "name": source_name,
                "url": url,
                "kind": "official-market-close",
                "observed_at": observed_at,
            }
            break
        if matched is None or source is None:
            return {"ok": False, "message": f"臺灣官方行情來源找不到：{symbol}"}

        events: list[dict[str, Any]] = []
        dividend_datasets = (
            (
                f"{TWSE_BASE}/exchangeReport/TWT48U_ALL",
                "Code",
                "CashDividend",
                "Date",
                "臺灣證券交易所",
            ),
            (
                f"{TPEX_BASE}/tpex_exright_daily",
                "SecuritiesCompanyCode",
                "CashDividend",
                "Date",
                "證券櫃檯買賣中心",
            ),
        )
        for url, code_key, amount_key, date_key, source_name in dividend_datasets:
            try:
                rows = self._cached_dataset(url, 3600)
            except (OSError, urllib.error.URLError, ValueError):
                continue
            for row in rows:
                if str(row.get(code_key) or "").strip().upper() != symbol:
                    continue
                amount = _number(row.get(amount_key), -1)
                event_at = _roc_date_iso(row.get(date_key))
                if amount < 0 or not event_at:
                    continue
                events.append(
                    {
                        "observed_at": event_at,
                        "record_date": event_at[:10],
                        "amount_per_unit": amount,
                        "currency": "TWD",
                        "source_name": source_name,
                        "source_url": url,
                    }
                )
        frequency = _infer_frequency(events)
        price = float(matched["price"])
        previous_close = price - change if price - change > 0 else None
        trailing_start = datetime.now(timezone.utc) - timedelta(days=366)
        trailing = sum(
            _number(item.get("amount_per_unit"))
            for item in events
            if datetime.fromisoformat(str(item["observed_at"])) >= trailing_start
        )
        return {
            "ok": True,
            "identity_key": f"{str(matched['exchange']).lower()}:{symbol}",
            "requested_symbol": symbol,
            "requested_name": str(holding.get("name") or ""),
            "resolved_symbol": symbol,
            "official_code": symbol,
            "isin": str(holding.get("isin") or ""),
            "name": str(holding.get("name") or symbol),
            "market": "TW",
            "asset_type": str(holding.get("asset_type") or "AUTO").upper(),
            "currency": "TWD",
            "quote_kind": "market_price",
            "observed_at": observed_at,
            "confidence": 0.96,
            "trusted": True,
            "parameters": {
                "price": price,
                "previous_close": previous_close,
                "change_percent": (change / previous_close * 100) if previous_close else None,
                "annual_distribution_per_unit": trailing,
                "distribution_yield_percent": (trailing / price * 100) if trailing > 0 else None,
                "distribution_frequency": frequency["code"],
            },
            "parameter_units": {
                "price": "currency",
                "previous_close": "currency",
                "change_percent": "%",
                "annual_distribution_per_unit": "currency",
                "distribution_yield_percent": "%",
            },
            "distribution": {
                "frequency": frequency["code"],
                "frequency_label": frequency["label"],
                "frequency_per_year": frequency["per_year"],
                "frequency_confidence": frequency["confidence"],
                "trailing_annual_per_unit": trailing,
                "event_count": len(events),
            },
            "distribution_events": events,
            "sources": [source],
        }

    @staticmethod
    def _merge_tw_official_result(
        yahoo: dict[str, Any], official: dict[str, Any]
    ) -> dict[str, Any]:
        merged = copy.deepcopy(yahoo)
        official_parameters = official.get("parameters") or {}
        parameters = merged.setdefault("parameters", {})
        for key in ("price", "previous_close", "change_percent"):
            if official_parameters.get(key) is not None:
                parameters[key] = official_parameters[key]
        merged["observed_at"] = official.get("observed_at") or merged.get("observed_at")
        merged["currency"] = official.get("currency") or merged.get("currency")
        merged["confidence"] = max(_number(merged.get("confidence")), 0.96)
        merged["trusted"] = True
        merged["identity_key"] = official.get("identity_key") or merged.get("identity_key")
        merged["official_code"] = official.get("official_code") or merged.get("official_code")
        sources = [
            *[item for item in official.get("sources", []) if isinstance(item, dict)],
            *[item for item in merged.get("sources", []) if isinstance(item, dict)],
        ]
        merged["sources"] = list(
            {
                (str(item.get("name") or ""), str(item.get("url") or "")): item
                for item in sources
            }.values()
        )
        official_events = [
            item for item in official.get("distribution_events", []) if isinstance(item, dict)
        ]
        yahoo_events = [
            item for item in merged.get("distribution_events", []) if isinstance(item, dict)
        ]
        events = list(
            {
                (
                    str(item.get("record_date") or item.get("observed_at") or ""),
                    _number(item.get("amount_per_unit"), -1),
                ): item
                for item in [*official_events, *yahoo_events]
            }.values()
        )
        merged["distribution_events"] = events
        frequency = _infer_frequency(events)
        trailing_start = datetime.now(timezone.utc) - timedelta(days=366)
        trailing = sum(
            _number(item.get("amount_per_unit"))
            for item in events
            if str(item.get("observed_at") or "")
            and datetime.fromisoformat(str(item["observed_at"])) >= trailing_start
        )
        price = _number(parameters.get("price"), 0)
        parameters["annual_distribution_per_unit"] = trailing
        parameters["distribution_yield_percent"] = (
            trailing / price * 100 if trailing > 0 and price > 0 else None
        )
        parameters["distribution_frequency"] = frequency["code"]
        merged["distribution"] = {
            "frequency": frequency["code"],
            "frequency_label": frequency["label"],
            "frequency_per_year": frequency["per_year"],
            "frequency_confidence": frequency["confidence"],
            "trailing_annual_per_unit": trailing,
            "event_count": len(events),
        }
        return merged
