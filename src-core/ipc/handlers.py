from __future__ import annotations

from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from main import GPTBridgeApp


TOOL_LIFECYCLE_HANDLERS = {
    "toolbox_list_tools": "list_tools",
    "toolbox_start_tool": "start_tool",
    "toolbox_stop_tool": "stop_tool",
    "toolbox_run_tool": "run_tool",
    "toolbox_cancel_tool_run": "cancel_tool_run",
}

MAIN_COMMANDS = {
    "toolbox_list_tools",
    "toolbox_start_tool",
    "toolbox_stop_tool",
    "app:get-runtime-status",
    "settings_health_refresh",
    "settings_mark_updates_applied",
}

STANDALONE_COMMANDS = {
    "toolbox_run_tool",
    "toolbox_cancel_tool_run",
    "app:get-runtime-status",
}


class CommandRouter:
    """Route only commands allowed by the current process boundary."""

    def __init__(
        self,
        app: "GPTBridgeApp",
        *,
        scope: str = "main",
        toolbox_service: Any = None,
        runtime_status_service: Any = None,
        update_service: Any = None,
        capability_services: dict[str, Any] | None = None,
    ) -> None:
        if scope not in {"main", "standalone"}:
            raise ValueError(f"unsupported runtime scope: {scope}")
        self.app = app
        self.scope = scope
        self.toolbox_service = toolbox_service
        self.runtime_status_service = runtime_status_service
        self.update_service = update_service
        self.capability_services = capability_services or {}
        self._log_reporter: Any = None

    def _capability_owner(self, command: str) -> Any | None:
        if self.scope != "standalone":
            return None
        for service in self.capability_services.values():
            try:
                if hasattr(service, "owns") and service.owns(command):
                    return service
            except Exception:
                continue
        return None

    async def handle(
        self, command: str, payload: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        capability_owner = self._capability_owner(command)
        allowed = MAIN_COMMANDS if self.scope == "main" else STANDALONE_COMMANDS
        if command not in allowed and capability_owner is None:
            return f"{command}_result", {
                "ok": False,
                "command": command,
                "error_code": "CAPABILITY_BOUNDARY_DENIED",
                "message": f"Command '{command}' is outside the {self.scope} process boundary.",
            }

        if capability_owner is not None:
            latest = payload.get("latest_ai_answer") if isinstance(payload, dict) else None
            return await capability_owner.handle(command, payload, latest)

        handler_name = TOOL_LIFECYCLE_HANDLERS.get(command)
        if handler_name is not None:
            if (
                self.scope == "main"
                and command == "toolbox_start_tool"
                and getattr(self.app, "maintenance_ready", True) is not True
            ):
                return f"{command}_result", {
                    "ok": False,
                    "queued": False,
                    "error_code": "STARTUP_MAINTENANCE_PENDING",
                    "message": "啟動維護尚未完成，指令未送出，請稍後再試。",
                }
            if self.toolbox_service is None:
                return f"{command}_result", {
                    "ok": False,
                    "message": "Tool lifecycle service is not available",
                }
            handler = getattr(self.toolbox_service, handler_name, None)
            if not callable(handler):
                return f"{command}_result", {
                    "ok": False,
                    "message": f"Tool lifecycle handler '{handler_name}' is unavailable",
                }
            result = (
                await handler()
                if command == "toolbox_list_tools"
                else await handler(payload)
            )
            return f"{command}_result", result

        if self.runtime_status_service and self.runtime_status_service.owns(command):
            return await self.runtime_status_service.handle(command, payload)

        if self.update_service and command in {
            "settings_health_refresh",
            "settings_mark_updates_applied",
        }:
            return await self.update_service.handle(command, payload)

        return f"{command}_result", {
            "ok": False,
            "message": f"Command '{command}' has no available handler.",
        }
