"""market-data domain — 星澄 read-only market data queries.

星澄 reads normalized market data through these commands over the
governed channel. Every result carries timestamps + source status —
the model can judge freshness itself. There are no write commands here:
the business layer cannot fabricate prices, and 星澄 cannot modify the
authoritative market data mirror.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ...domain.contract import DOMAIN_MARKET_DATA
from .base import BusinessDomain


class MarketDataDomain(BusinessDomain):
    domain_id = DOMAIN_MARKET_DATA
    label = "行情資料中心"
    commands = frozenset(
        {
            "investment_market_quote",
            "investment_market_history",
            "investment_market_trend",
            "investment_market_sources",
            "investment_market_status",
        }
    )

    async def handle(self, command, payload, *, store, ai_connections):
        if command == "investment_market_quote":
            quote = store.market_quote(str(payload.get("instrument_id") or ""))
            if quote is None:
                return {"ok": False, "error_code": "NO_QUOTE"}
            return {"ok": True, "domain": self.domain_id, "quote": quote}

        if command == "investment_market_history":
            candles = store.market_candles(
                str(payload.get("instrument_id") or ""),
                timeframe=str(payload.get("timeframe") or "1d"),
                limit=int(payload.get("limit") or 500),
                adjustment_type=str(payload.get("adjustment_type") or "raw"),
            )
            return {
                "ok": True,
                "domain": self.domain_id,
                "adjustment_type": str(payload.get("adjustment_type") or "raw"),
                "candles": candles,
                "count": len(candles),
            }

        if command == "investment_market_trend":
            candles = store.market_candles(
                str(payload.get("instrument_id") or ""),
                timeframe="1d",
                limit=int(payload.get("days") or 20),
            )
            if not candles:
                return {"ok": False, "error_code": "NO_HISTORY"}
            try:
                first = Decimal(str(candles[0]["close"]))
                last = Decimal(str(candles[-1]["close"]))
            except (InvalidOperation, KeyError, IndexError):
                return {"ok": False, "error_code": "BAD_HISTORY"}
            volumes = [
                Decimal(str(c["volume"])) for c in candles if c.get("volume")
            ]
            return {
                "ok": True,
                "domain": self.domain_id,
                "days": len(candles),
                "first_close": str(first),
                "last_close": str(last),
                "change_pct": str((last - first) / first * 100) if first else None,
                "avg_volume": str(sum(volumes) / len(volumes)) if volumes else None,
                "latest_source": candles[-1].get("source_id"),
                "latest_candle": candles[-1].get("candle_start"),
            }

        if command == "investment_market_sources":
            return {
                "ok": True,
                "domain": self.domain_id,
                "sources": store.market_source_statuses(),
            }

        if command == "investment_market_status":
            return {
                "ok": True,
                "domain": self.domain_id,
                "sources": store.market_source_statuses(),
                "note": "行情引擎位於 investment-mobile 工具邊界；此為權威鏡像狀態",
            }

        raise PermissionError("PERMISSION_DENIED")
