"""AI Nexus service — 星澄 AI 投資管理與自動操盤系統 business layer.

Owns the six business domains, the canonical trading store, the
investment-mobile governed bridge, and the 星澄 AI connections surface.
The legacy investment-manager implementation was removed; this service
is a fresh architecture per the rebuild mandate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.contract import DOMAIN_IDS, TRADING_APP_VERSION
from ..infrastructure.trading_store import TradingStore
from ..integration.star_channel import InvestmentAiConnections
from .domains import build_domains
from .mobile_bridge import InvestmentMobileBridge


class AiNexusService:
    """Business-layer service for the rebuilt trading system."""

    VERSION = TRADING_APP_VERSION

    MOBILE_ENVELOPE_COMMANDS = {
        "investment_mobile_get_snapshot",
        "investment_mobile_submit_instruction",
    }
    AI_CONNECTION_COMMANDS = {
        "investment_ai_connections_status",
        "investment_ai_consult",
        "investment_star_memory_list",
        "investment_star_memory_review",
    }
    BOOKKEEPING_COMMANDS = {
        "investment_status",
        "investment_domains",
    }

    def __init__(self, project_root: Path, session: Any | None = None) -> None:
        del session
        self.project_root = Path(project_root)
        self.ai_connections = InvestmentAiConnections()
        self.store = TradingStore(self.project_root)
        self.bridge = InvestmentMobileBridge(self.store, self.ai_connections)
        self.domains = build_domains()
        domain_commands: set[str] = set()
        for domain in self.domains:
            domain_commands |= set(domain.commands)
        self.DOMAIN_COMMANDS = domain_commands
        self.COMMANDS = (
            domain_commands
            | self.MOBILE_ENVELOPE_COMMANDS
            | self.AI_CONNECTION_COMMANDS
            | self.BOOKKEEPING_COMMANDS
        )

    def owns(self, command: str) -> bool:
        return str(command) in self.COMMANDS

    async def start(self) -> None:
        self.store.open()

    async def shutdown(self) -> None:
        self.store.close()

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        latest_ai_answer: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        del latest_ai_answer
        command = str(command)
        payload = dict(payload or {})

        for domain in self.domains:
            if domain.owns(command):
                result = await domain.handle(
                    command,
                    payload,
                    store=self.store,
                    ai_connections=self.ai_connections,
                )
                return f"{command}_result", result

        if command == "investment_mobile_get_snapshot":
            return f"{command}_result", await self.bridge.snapshot(payload)
        if command == "investment_mobile_submit_instruction":
            return f"{command}_result", await self.bridge.submit_instruction(payload)

        if command == "investment_status":
            return f"{command}_result", {
                "ok": True,
                "product": "星澄 AI 投資管理與自動操盤系統",
                "version": self.VERSION,
                "domains": list(DOMAIN_IDS),
                "store": str(self.store.database_path),
                "ai_connections": self.ai_connections.status(),
            }
        if command == "investment_domains":
            return f"{command}_result", {
                "ok": True,
                "domains": [
                    {"id": d.domain_id, "label": d.label, "commands": sorted(d.commands)}
                    for d in self.domains
                ],
            }

        if command == "investment_ai_connections_status":
            return f"{command}_result", self.ai_connections.status()
        if command == "investment_ai_consult":
            result = await self.ai_connections.consult(
                str(payload.get("prompt") or ""),
                str(payload.get("scope") or "local"),
            )
            return f"{command}_result", result
        if command == "investment_star_memory_list":
            result = self.ai_connections.list_star_memory_sync(
                include_inactive=payload.get("include_inactive") is True,
                limit=int(payload.get("limit") or 100),
            )
            return f"{command}_result", result
        if command == "investment_star_memory_review":
            result = self.ai_connections.review_star_memory_sync(
                str(payload.get("memory_id") or ""),
                action=str(payload.get("action") or ""),
                reviewer=str(payload.get("reviewer") or "investment-owner"),
                reason=str(payload.get("reason") or ""),
            )
            return f"{command}_result", result

        raise PermissionError("PERMISSION_DENIED")
