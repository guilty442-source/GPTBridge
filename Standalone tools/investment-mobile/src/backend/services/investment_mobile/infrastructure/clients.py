"""Investment Mobile Infrastructure Layer - External services."""

from __future__ import annotations

from typing import Any


class MarketDataClient:
    """Client for fetching market data."""

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key

    async def fetch_price(self, symbol: str) -> dict[str, Any]:
        """Fetch current price for a symbol."""
        return {"symbol": symbol, "price": 0.0, "source": "mock"}


class DatabaseClient:
    """Database client for investment data."""

    def __init__(self, connection_string: str) -> None:
        self.connection_string = connection_string

    async def save_portfolio(self, portfolio: dict[str, Any]) -> bool:
        """Save portfolio to database."""
        return True

    async def load_portfolio(self, portfolio_id: str) -> dict[str, Any] | None:
        """Load portfolio from database."""
        return None