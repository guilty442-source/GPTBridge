"""Business domain base — command dispatch contract.

Each domain owns a fixed command surface and receives the canonical
``TradingStore`` plus the governed ``InvestmentAiConnections`` handle
(星澄 analysis requests travel through the AI channel; domains never
reach models or networks directly).
"""

from __future__ import annotations

from typing import Any

from ...infrastructure.trading_store import TradingStore


class BusinessDomain:
    domain_id = ""
    label = ""
    commands: frozenset[str] = frozenset()

    def owns(self, command: str) -> bool:
        return str(command) in self.commands

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        store: TradingStore,
        ai_connections: Any,
    ) -> dict[str, Any]:
        raise PermissionError("PERMISSION_DENIED")
