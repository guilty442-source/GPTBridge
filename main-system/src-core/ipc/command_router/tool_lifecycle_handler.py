"""Command Router — Tool Lifecycle Handlers."""

from __future__ import annotations

from typing import Any, Dict

from .constants import TOOL_LIFECYCLE_HANDLERS


class ToolLifecycleHandler:
    """Handle toolbox_* commands."""

    def __init__(self, app: Any, toolbox_service: Any = None) -> None:
        self.app = app
        self.toolbox_service = toolbox_service
        self.scope = "main"

    async def handle(self, command: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        handler_name = TOOL_LIFECYCLE_HANDLERS.get(command)
        if handler_name is None:
            return f"{command}_result", {
                "ok": False,
                "message": f"Command '{command}' has no available handler.",
            }

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
        if command == "toolbox_list_tools":
            return f"{command}_result", await handler()
        if command in ("toolbox_request_tool_execution", "toolbox_run_tool"):
            governed_command = (
                str(payload.get("command") or "").strip() or command
            )
            payload = {**payload, "_governed_command": governed_command}
        result = await handler(payload)
        return f"{command}_result", result
