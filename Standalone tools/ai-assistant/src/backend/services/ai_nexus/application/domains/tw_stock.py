"""台灣股票 domain — 國泰綜合證券 market boundary."""

from __future__ import annotations

from typing import Any

from ...domain.contract import DOMAIN_TW_STOCK
from .base import BusinessDomain


class TwStockDomain(BusinessDomain):
    domain_id = DOMAIN_TW_STOCK
    label = "台灣股票"
    market = "tw"
    broker_adapter = "cathay-tw"
    commands = frozenset(
        {
            "investment_tw_portfolio",
            "investment_tw_analysis",
            "investment_tw_recommendations",
        }
    )

    async def handle(self, command, payload, *, store, ai_connections):
        if command == "investment_tw_portfolio":
            return {
                "ok": True,
                "domain": self.domain_id,
                "positions": store.positions(market=self.market),
                "recent_orders": store.orders(market=self.market, limit=20),
            }
        if command == "investment_tw_analysis":
            # 星澄 produces analysis through the governed AI channel.
            result = await ai_connections.consult(
                str(
                    payload.get("prompt")
                    or "分析台灣股票市場趨勢與目前持倉曝險"
                ),
                "investment-analysis",
            )
            return {"ok": True, "domain": self.domain_id, "analysis": result}
        if command == "investment_tw_recommendations":
            return {
                "ok": True,
                "domain": self.domain_id,
                "recommendations": store.signals(market=self.market, limit=50),
                "note": "星澄候選訊號；正式決策屬於獨立策略與風控引擎",
            }
        raise PermissionError("PERMISSION_DENIED")
