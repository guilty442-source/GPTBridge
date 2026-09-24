from __future__ import annotations

import asyncio
import json
import time
import uuid
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


def _service_version() -> str:
    try:
        from shared_layer.registry.versioning import component_version

        return component_version("ai-collaboration")
    except Exception:
        pass
    manifest_path = Path(__file__).resolve().parents[5] / "manifest.json"
    try:
        version = str(
            json.loads(manifest_path.read_text("utf-8")).get("version", "")
        ).strip()
        if version:
            return version
    except Exception:
        pass
    return "1.0.0"


def _resolve_tool_root(project_root: Path) -> Path:
    root = Path(project_root).resolve()
    # Candidates in precedence order: the tool root itself, a direct child
    # (callers that pass "Standalone tools"), then the governed repo
    # layout ("Standalone tools/ai-collaboration").  The manifest check
    # prevents a stray same-named directory from being mistaken for the
    # tool root.
    for candidate in (
        root,
        root / "ai-collaboration",
        root / "Standalone tools" / "ai-collaboration",
    ):
        if (
            candidate.name == "ai-collaboration"
            and (candidate / "manifest.json").is_file()
        ):
            return candidate.resolve()
    # Tests and embedded callers may pass a bare sandbox root — keep the
    # legacy fallthrough so runtime/state still lands under it.
    return root


class AiCollaborationService(
    CollabSvcAgentsMixin,
    CollabSvcMessagingMixin,
    CollabSvcCoordinationMixin,
    CollabSvcBrowserMixin,
    CollabSvcMemoryMixin,
    CollabSvcDiagnosticsMixin,
):
    VERSION = _service_version()
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
        "ai_nexus_add_agent",
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
        # Runtime generation + request correlation (one click → one send).
        self._runtime_generation = uuid.uuid4().hex[:12]
        self._request_messages: dict[str, str] = {}
        self._send_inflight: dict[str, asyncio.Task] = {}
        self._send_results: dict[str, tuple[float, dict[str, Any]]] = {}

    @property
    def runtime_generation(self) -> str:
        return self._runtime_generation

    def is_ready(self) -> bool:
        try:
            self.repository.list_agents()
            return True
        except Exception:
            return False

    async def cancel(self, request_id: str) -> bool:
        """Cancellation callback for GovernedToolRuntime / toolbox_cancel_tool_run."""
        request_id = str(request_id or "").strip()
        if not request_id:
            return False
        message_id = self._request_messages.get(request_id)
        if not message_id:
            return False
        cancelled = self.repository.cancel_pending_responses(message_id)
        for key, future in list(self._browser_completion.items()):
            if key[0] == message_id and not future.done():
                future.cancel()
        for agent in self.repository.list_agents():
            if str(agent.get("status") or "") in {
                "running",
                "awaiting-user",
                "waiting",
            }:
                self.repository.update_agent_status(
                    str(agent.get("agent_id") or ""), "idle"
                )
        return bool(cancelled)

    def _track_send(self, request_id: str, message_id: str) -> None:
        if request_id and message_id:
            self._request_messages[request_id] = message_id
            if len(self._request_messages) > 256:
                self._request_messages.clear()

    def _send_dedupe_hit(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """Return the recorded result when the same idempotency key was
        already accepted — one user click must never send twice."""
        key = str(payload.get("idempotency_key") or "").strip()
        if not key:
            return None
        now = time.monotonic()
        recorded = self._send_results.get(key)
        if recorded is not None and now - recorded[0] < 120:
            result = dict(recorded[1])
            result["deduplicated"] = True
            return result
        return None

    def _record_send_result(self, payload: dict[str, Any], result: dict[str, Any]) -> None:
        key = str(payload.get("idempotency_key") or "").strip()
        if not key:
            return
        self._send_results[key] = (time.monotonic(), dict(result))
        if len(self._send_results) > 64:
            oldest = sorted(self._send_results.items(), key=lambda item: item[1][0])[:16]
            for old_key, _ in oldest:
                self._send_results.pop(old_key, None)

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
            "ai_nexus_add_agent": self._add_agent,
            "ai_nexus_update_agent_business_settings": self._update_agent_business_settings,
            "ai_nexus_send_message": self._send_message,
            "ai_nexus_complete_browser_response": self._complete_browser_response,
            "ai_nexus_add_memory": self._add_memory,
            "ai_nexus_create_task": self._create_task,
            "ai_nexus_export_report": self._export_report,
        }
        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {
                "ok": False,
                "error_code": "UNSUPPORTED_COMMAND",
                "message": "不支援的 AI 協作指令",
            }
        request_id = str(payload.get("_governed_request_id") or "").strip()
        if request_id:
            payload["request_id"] = request_id
        try:
            result = await handler(payload)
        except PermissionError:
            result = {"ok": False, "message": "PERMISSION_DENIED"}
        except Exception as exc:
            result = {"ok": False, "message": str(exc)}
        if (
            isinstance(result, dict)
            and result.get("ok") is False
            and not str(result.get("error_code") or "").strip()
        ):
            result = dict(result)
            result["error_code"] = (
                "PERMISSION_DENIED"
                if result.get("message") == "PERMISSION_DENIED"
                else "REQUEST_REJECTED"
            )
        return f"{command}_result", result

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
