"""Investment Mobile Application Layer - Use cases.

All portfolio state is owned by ai-assistant; these use cases relay through
the governed AI channel (via ``ChannelClient`` -> xingcheng -> ai-assistant)
and only fall back to the injected clients when the channel is not bound.
"""

from __future__ import annotations

from typing import Any


class AnalyzeInvestmentUseCase:
    """Use case for investment analysis."""

    def __init__(
        self,
        market_client: Any,
        db_client: Any,
        channel_client: Any | None = None,
    ) -> None:
        self.market_client = market_client
        self.db_client = db_client
        self.channel_client = channel_client

    async def execute(self, portfolio_id: str) -> dict[str, Any]:
        """Analyze investment portfolio via the governed channel."""
        if self.channel_client is not None and self.channel_client.connected:
            return await self.channel_client.snapshot(
                {"portfolio_id": portfolio_id}
            )
        portfolio = (
            await self.db_client.load_portfolio(portfolio_id)
            if self.db_client is not None
            else None
        )
        if not portfolio:
            return {
                "ok": False,
                "error_code": "PORTFOLIO_NOT_FOUND",
                "portfolio_id": portfolio_id,
            }
        return {
            "ok": True,
            "portfolio_id": portfolio_id,
            "portfolio": portfolio,
        }


class ManagePortfolioUseCase:
    """Use case for portfolio management."""

    def __init__(self, db_client: Any, channel_client: Any | None = None) -> None:
        self.db_client = db_client
        self.channel_client = channel_client

    async def create_portfolio(
        self, name: str, assets: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Create a new portfolio through the governed channel."""
        portfolio = {
            "id": f"portfolio-{name}",
            "name": name,
            "assets": assets,
        }
        if self.channel_client is not None and self.channel_client.connected:
            return await self.channel_client.submit_instruction(
                {
                    "operation": "manage_portfolio",
                    "action": "create",
                    "portfolio": portfolio,
                    "instruction": f"建立投資組合 {name}",
                }
            )
        if self.db_client is not None:
            await self.db_client.save_portfolio(portfolio)
        return {"ok": True, "portfolio": portfolio}
