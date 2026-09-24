"""共同基金 domain — extensible multi-platform boundary."""

from __future__ import annotations

from ...domain.contract import DOMAIN_FUND
from .base import BusinessDomain


class FundDomain(BusinessDomain):
    domain_id = DOMAIN_FUND
    label = "共同基金"
    market = "fund"
    commands = frozenset(
        {
            "investment_fund_portfolio",
            "investment_fund_recommendations",
            "investment_fund_platforms",
        }
    )

    async def handle(self, command, payload, *, store, ai_connections):
        if command == "investment_fund_portfolio":
            return {
                "ok": True,
                "domain": self.domain_id,
                "positions": store.positions(market=self.market),
                "recent_orders": store.orders(market=self.market, limit=20),
            }
        if command == "investment_fund_recommendations":
            # 申購/贖回建議 — 星澄候選訊號清單
            return {
                "ok": True,
                "domain": self.domain_id,
                "recommendations": store.signals(market=self.market, limit=50),
                "note": "申購/贖回候選訊號由星澄產生，正式決策屬於策略與風控引擎",
            }
        if command == "investment_fund_platforms":
            platforms = store.kv_get(self.domain_id, "platforms", [])
            return {
                "ok": True,
                "domain": self.domain_id,
                "platforms": platforms,
                "extensible": True,
                "note": "多平台整合預留；接入前需平台 API 驗證",
            }
        raise PermissionError("PERMISSION_DENIED")
