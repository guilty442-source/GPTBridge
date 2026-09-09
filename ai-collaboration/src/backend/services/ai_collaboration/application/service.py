from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..infrastructure.repository import AiCollaborationRepository
from .collab_svc_agents import CollabSvcAgentsMixin
from .collab_svc_messaging import CollabSvcMessagingMixin
from .collab_svc_coordination import CollabSvcCoordinationMixin
from .collab_svc_browser import CollabSvcBrowserMixin
from .collab_svc_memory import CollabSvcMemoryMixin
from .collab_svc_diagnostics import CollabSvcDiagnosticsMixin

__all__ = ["AiCollaborationService"]


def _resolve_tool_root(project_root: Path) -> Path:
    if project_root.name == "ai-collaboration" and (project_root / "manifest.json").is_file():
        return project_root.resolve()
    candidate = project_root / "ai-collaboration"
    if candidate.exists():
        return candidate.resolve()
    return project_root.resolve()


class AiCollaborationService(
    CollabSvcAgentsMixin,
    CollabSvcMessagingMixin,
    CollabSvcCoordinationMixin,
    CollabSvcBrowserMixin,
    CollabSvcMemoryMixin,
    CollabSvcDiagnosticsMixin,
):
    VERSION = "1.00000"
    MAX_PARALLEL_AI = 6
    BROWSER_WAIT_CYCLES = 3
    BROWSER_WAIT_SECONDS = 20
    FIXED_TASK_OWNERS = {
        "general": "chatgpt",
        "orchestration": "chatgpt",
        "search": "gemini",
        "advanced_search": "perplexity",
        "calculation": "perplexity",
        "longform": "claude",
        "reasoning": "deepseek",
        "social_media": "grok",
        "trends": "grok",
        "breaking_news": "grok",
        "training-candidate-authoring": "chatgpt",
    }
    COMMANDS = {
        "ai_nexus_get_state",
        "ai_nexus_open_agent",
        "ai_nexus_authorize_agent",
        "ai_nexus_open_selected_agents",
        "ai_nexus_set_agent_selection",
        "ai_nexus_update_agent_business_settings",
        "ai_nexus_send_message",
        "ai_nexus_complete_browser_response",
        "ai_nexus_add_memory",
        "ai_nexus_create_task",
        "ai_nexus_export_report",
    }

    def __init__(self, project_root: Path, session: Any | None = None) -> None:
        self.project_root = project_root.resolve()
        self.tool_root = _resolve_tool_root(project_root)
        self.repository = AiCollaborationRepository(self.tool_root)
        self._owns_session = session is None
        if session is None:
            from ..integration.provider_session import AiCollaborationProviderSession

            self.session = AiCollaborationProviderSession(self.project_root)
        else:
            self.session = session
        self._send_lock = asyncio.Lock()
        self._task_slots = asyncio.Semaphore(self.MAX_PARALLEL_AI)
        self._browser_completion: dict[tuple[str, str], asyncio.Future[str]] = {}

    @property
    def workspace(self) -> Any:
        tool_root = self.tool_root

        class Workspace:
            workspace_root = tool_root

        return Workspace()

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        if self._owns_session and hasattr(self.session, "shutdown"):
            await self.session.shutdown()

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        latest_ai_answer: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        del latest_ai_answer
        handlers = {
            "ai_nexus_get_state": self._get_state,
            "ai_nexus_open_agent": self._open_agent,
            "ai_nexus_authorize_agent": self._authorize_agent,
            "ai_nexus_open_selected_agents": self._open_selected_agents,
            "ai_nexus_set_agent_selection": self._set_agent_selection,
            "ai_nexus_update_agent_business_settings": self._update_agent_business_settings,
            "ai_nexus_send_message": self._send_message,
            "ai_nexus_complete_browser_response": self._complete_browser_response,
            "ai_nexus_add_memory": self._add_memory,
            "ai_nexus_create_task": self._create_task,
            "ai_nexus_export_report": self._export_report,
        }
        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {"ok": False, "message": "不支援的 AI 協作指令"}
        try:
            return f"{command}_result", await handler(payload)
        except Exception as exc:
            return f"{command}_result", {"ok": False, "message": str(exc)}

    @staticmethod
    def _requester_tool_id(payload: dict[str, Any]) -> str:
        actor = str(payload.get("_authorized_requester_actor") or "").strip()
        if actor == "governance/tool/ai-assistant":
            raise PermissionError("PERMISSION_DENIED")
        if actor == "governance/tool/xingcheng":
            return "xingcheng"
        if actor in {"governance/tool/ai-collaboration", "governance/main-system"}:
            return "ai-collaboration"
        # Direct calls are limited to in-process tests; governed runtimes always
        # supply a verified actor before reaching the service.
        if not actor:
            return "xingcheng"
        raise PermissionError("PERMISSION_DENIED")
