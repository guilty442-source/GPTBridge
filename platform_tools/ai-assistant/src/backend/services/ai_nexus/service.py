from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .investment_contract import INVESTMENT_APP_VERSION
from .investment_watch import InvestmentWatchService
from .ai_connections import InvestmentAiConnections


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
    }
    COMMANDS = INVESTMENT_COMMANDS | AI_CONNECTION_COMMANDS

    def __init__(self, project_root: Path, session: Any | None = None) -> None:
        del session
        self.project_root = project_root
        self.ai_connections = InvestmentAiConnections()
        managed_tool_id = str(
            os.environ.get("GPTBRIDGE_STANDALONE_TOOL_ID")
            or os.environ.get("GPTBRIDGE_TOOL_ID")
            or ""
        ).strip()
        self.investment_service = InvestmentWatchService(
            project_root,
            None,
            ai_connections=(
                self.ai_connections if managed_tool_id == "ai-assistant" else None
            ),
        )

    @property
    def workspace(self) -> Any:
        class Workspace:
            workspace_root = self.project_root / "platform_tools" / "ai-assistant"

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
                str(payload.get("scope") or "both"),
            )
            return f"{command}_result", result
        return f"{command}_result", {
            "ok": False,
            "message": "不支援的投資管家指令。",
        }
