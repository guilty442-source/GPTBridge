"""Investment Mobile Infrastructure Layer - External services.

investment-mobile owns no network route and no database (manifest denies
``separate-business-database`` and ``direct-database-write``); both clients
delegate to ai-assistant through the governed AI channel.
"""

from __future__ import annotations

from typing import Any


class MarketDataClient:
    """Client for fetching market data via the governed channel."""

    def __init__(
        self,
        api_key: str | None = None,
        channel_client: Any | None = None,
    ) -> None:
        self.api_key = api_key
        self._channel_client = channel_client

    async def fetch_price(self, symbol: str) -> dict[str, Any]:
        """Fetch current price for a symbol through ai-assistant."""
        if self._channel_client is None or not self._channel_client.connected:
            return {
                "ok": False,
                "error_code": "NETWORK_ACCESS_DISABLED",
                "symbol": symbol,
                "source": "governed-channel",
            }
        result = await self._channel_client.submit_instruction(
            {
                "operation": "market_search",
                "symbols": [str(symbol)],
                "instruction": f"market-search {symbol}",
            }
        )
        return {
            "ok": result.get("ok") is not False,
            "symbol": symbol,
            "source": "ai-assistant",
            "quote": result,
        }


class DatabaseClient:
    """Persistence facade delegating to ai-assistant (canonical owner)."""

    persistence_owner = "ai-assistant"

    def __init__(
        self,
        connection_string: str,
        channel_client: Any | None = None,
    ) -> None:
        self.connection_string = connection_string
        self._channel_client = channel_client

    async def save_portfolio(self, portfolio: dict[str, Any]) -> bool:
        """Persist portfolio through the governed channel."""
        if self._channel_client is None or not self._channel_client.connected:
            return False
        result = await self._channel_client.submit_instruction(
            {
                "operation": "manage_portfolio",
                "action": "save",
                "portfolio": dict(portfolio),
                "instruction": (
                    f"儲存投資組合 {portfolio.get('name') or portfolio.get('id') or ''}"
                ),
            }
        )
        return result.get("ok") is not False

    async def load_portfolio(self, portfolio_id: str) -> dict[str, Any] | None:
        """Load the shared portfolio snapshot through the governed channel."""
        if self._channel_client is None or not self._channel_client.connected:
            return None
        snapshot = await self._channel_client.snapshot(
            {"portfolio_id": portfolio_id}
        )
        if snapshot.get("ok") is False:
            return None
        return snapshot
