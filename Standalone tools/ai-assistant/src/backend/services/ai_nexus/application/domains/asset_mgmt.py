"""全資產管理 domain — cross-market aggregation."""

from __future__ import annotations

from collections import defaultdict

from ...domain.contract import DOMAIN_ASSET_MGMT
from .base import BusinessDomain


class AssetManagementDomain(BusinessDomain):
    domain_id = DOMAIN_ASSET_MGMT
    label = "全資產管理"
    commands = frozenset(
        {
            "investment_assets_summary",
            "investment_assets_holdings",
            "investment_assets_transactions",
        }
    )

    async def handle(self, command, payload, *, store, ai_connections):
        if command == "investment_assets_summary":
            positions = store.positions()
            by_market: dict[str, float] = defaultdict(float)
            for position in positions:
                by_market[position["market"]] += (
                    position["quantity"] * position.get("average_cost", 0.0)
                )
            return {
                "ok": True,
                "domain": self.domain_id,
                "markets": dict(by_market),
                "total_cost_basis": sum(by_market.values()),
                "position_count": len(positions),
            }
        if command == "investment_assets_holdings":
            return {
                "ok": True,
                "domain": self.domain_id,
                "holdings": store.positions(),
            }
        if command == "investment_assets_transactions":
            return {
                "ok": True,
                "domain": self.domain_id,
                "orders": store.orders(limit=int(payload.get("limit") or 200)),
                "fills": store.fills(limit=int(payload.get("limit") or 200)),
            }
        raise PermissionError("PERMISSION_DENIED")
