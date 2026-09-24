"""美國股票 domain — 富邦證券複委託 market boundary."""

from __future__ import annotations

from ...domain.contract import DOMAIN_US_STOCK
from .base import BusinessDomain


class UsStockDomain(BusinessDomain):
    domain_id = DOMAIN_US_STOCK
    label = "美國股票"
    market = "us"
    broker_adapter = "fubon-us-sub"
    commands = frozenset(
        {
            "investment_us_portfolio",
            "investment_us_analysis",
            "investment_us_recommendations",
        }
    )

    async def handle(self, command, payload, *, store, ai_connections):
        if command == "investment_us_portfolio":
            return {
                "ok": True,
                "domain": self.domain_id,
                "positions": store.positions(market=self.market),
                "recent_orders": store.orders(market=self.market, limit=20),
            }
        if command == "investment_us_analysis":
            result = await ai_connections.consult(
                str(
                    payload.get("prompt")
                    or "分析美國股票市場趨勢與目前持倉曝險"
                ),
                "investment-analysis",
            )
            return {"ok": True, "domain": self.domain_id, "analysis": result}
        if command == "investment_us_recommendations":
            return {
                "ok": True,
                "domain": self.domain_id,
                "recommendations": store.signals(market=self.market, limit=50),
                "note": "星澄候選訊號；正式決策屬於獨立策略與風控引擎",
            }
        raise PermissionError("PERMISSION_DENIED")
