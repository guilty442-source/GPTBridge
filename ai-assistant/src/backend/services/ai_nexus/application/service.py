from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.contract import INVESTMENT_APP_VERSION
from .investment_watch import InvestmentWatchService
from ..integration.star_channel import InvestmentAiConnections


class AiNexusService:
    """AI投資管家 service.

    The historical package name stays as ``ai_nexus`` so existing loaders can
    still discover the service, but this application now owns only local
    investment commands. External AI collaboration lives in the independent
    ``ai-collaboration`` platform tool.
    """

    VERSION = INVESTMENT_APP_VERSION
    INVESTMENT_COMMANDS = set(InvestmentWatchService.COMMANDS)
    AI_CONNECTION_COMMANDS = {
        "investment_ai_connections_status",
        "investment_ai_consult",
        "investment_star_memory_list",
        "investment_star_memory_review",
    }
    COMMANDS = INVESTMENT_COMMANDS | AI_CONNECTION_COMMANDS

    def __init__(self, project_root: Path, session: Any | None = None) -> None:
        del session
        self.project_root = project_root
        self.ai_connections = InvestmentAiConnections()
        self.investment_service = InvestmentWatchService(
            project_root,
            None,
            ai_connections=self.ai_connections,
        )

    @property
    def workspace(self) -> Any:
        class Workspace:
            workspace_root = self.project_root

        return Workspace()

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def start(self) -> None:
        if hasattr(self.investment_service, "start"):
            await self.investment_service.start()

    async def shutdown(self) -> None:
        if hasattr(self.investment_service, "shutdown"):
            await self.investment_service.shutdown()

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        latest_ai_answer: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        del latest_ai_answer
        if command in self.INVESTMENT_COMMANDS:
            return await self.investment_service.handle(command, payload)
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
                reviewer=str(payload.get("reviewer") or "investment-manager-owner"),
                reason=str(payload.get("reason") or ""),
            )
            return f"{command}_result", result
        return f"{command}_result", {
            "ok": False,
            "message": "不支援的投資管家指令。",
        }
