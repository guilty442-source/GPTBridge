from __future__ import annotations

from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from main import GPTBridgeApp


TOOL_LIFECYCLE_HANDLERS = {
    "toolbox_list_tools": "list_tools",
    "toolbox_start_tool": "start_tool",
    "toolbox_stop_tool": "stop_tool",
    "toolbox_force_close_tool": "force_close_tool",
    "toolbox_request_tool_execution": "request_tool_execution",
    "toolbox_cancel_tool_execution": "cancel_tool_execution",
}

MAIN_COMMANDS = {
    "toolbox_list_tools",
    "toolbox_start_tool",
    "toolbox_stop_tool",
    "toolbox_force_close_tool",
    "toolbox_request_tool_execution",
    "toolbox_cancel_tool_execution",
    "app:get-runtime-status",
    "app:get-governance-rules",
}

class CommandRouter:
    """Route only commands allowed by the current process boundary."""

    def __init__(
        self,
        app: "GPTBridgeApp",
        *,
        toolbox_service: Any = None,
        runtime_status_service: Any = None,
    ) -> None:
        self.app = app
        self.scope = "main"
        self.toolbox_service = toolbox_service
        self.runtime_status_service = runtime_status_service
        self._log_reporter: Any = None

    async def handle(
        self, command: str, payload: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        if command not in MAIN_COMMANDS:
            return f"{command}_result", {
                "ok": False,
                "command": command,
                "error_code": "CAPABILITY_BOUNDARY_DENIED",
                "message": "PERMISSION_DENIED",
            }

        if command == "app:get-governance-rules":
            rules = list(self.app.governance_rules)
            return f"{command}_result", {
                "ok": True,
                "rules": rules,
                "available_rules": rules,
                "active_rules": rules,
                "authority": "governance-rule-only",
                "runtime_mutation": "prohibited",
                "command_strategy": {
                    "model": "governance-authenticated-shared-layer-channel-only",
                    "flow": [
                        "request",
                        "governance-authentication",
                        "shared-layer-queue",
                        "owner-tool-claim",
                        "owner-tool-execution",
                        "shared-layer-response",
                        "result",
                    ],
                    "global-cleaner": [
                        "global-garbage-cleanup",
                        "managed-backup-per-owner-retention",
                        "governed-backup-extraction",
                        "delete-excess-logs",
                        "read-only-system-health-check",
                    ],
                    "system-rescue": [
                        "managed-storage-repair",
                        "write-rescue-audit-and-log",
                    ],
                    "requester_write_authority": "none",
                    "direct_main_to_tool_instruction": "PERMISSION_DENIED",
                    "direct_tool_to_tool_instruction": "PERMISSION_DENIED",
                    "automatic_repair": (
                        "governance-authorized-stability-only-with-optional-"
                        "global-cleaner-backup-extraction-via-shared-layer"
                    ),
                    "unauthorized_result": "PERMISSION_DENIED",
                },
            }

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

        return f"{command}_result", {
            "ok": False,
            "message": f"Command '{command}' has no available handler.",
        }
