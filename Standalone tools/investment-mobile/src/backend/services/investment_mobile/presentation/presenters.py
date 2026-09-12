"""Investment Mobile Presentation Layer - UI/API interfaces."""

from __future__ import annotations

from typing import Any


class InvestmentMobilePresenter:
    """Presenter for investment mobile UI."""

    def __init__(self, use_case: Any) -> None:
        self.use_case = use_case

    async def present_analysis(self, portfolio_id: str) -> dict[str, Any]:
        """Present investment analysis."""
        result = await self.use_case.execute(portfolio_id)
        return {
            "status": "success",
            "data": result,
        }


class PortfolioPresenter:
    """Presenter for portfolio management."""

    def __init__(self, use_case: Any) -> None:
        self.use_case = use_case

    async def present_create(self, name: str, assets: list[dict[str, Any]]) -> dict[str, Any]:
        """Present portfolio creation."""
        portfolio = await self.use_case.create_portfolio(name, assets)
        return {
            "status": "created",
            "portfolio": portfolio,
        }