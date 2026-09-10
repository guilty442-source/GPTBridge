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
    "app:run-main-system-self-maintenance",
    "app:get-third-party-status",
    "app:probe-third-party-versions",
    "app:check-third-party-updates",
    "app:update-third-party-tool",
    "app:auto-update-third-party-tools",
    "app:get-repair-status",
    "app:hot-reload-backend",
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

    def _get_third_party_sovereign(self) -> Any:
        """Resolve the third-party sub-sovereign from the system sovereign."""
        system_sovereign = getattr(self.app, "system_sovereign_service", None)
        if system_sovereign is None:
            return None
        return getattr(system_sovereign, "third_party_sovereign", None)

    def _get_maintenance_sovereign(self) -> Any:
        """Resolve the system-health owner (maintenance sovereign)."""
        return getattr(self.app, "maintenance_sovereign", None)

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
                    "main-system-central-repair": [
                        "managed-storage-repair",
                        "write-repair-audit-and-log",
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

        if command == "app:run-main-system-self-maintenance":
            service = getattr(self.app, "main_system_self_maintenance", None)
            if service is None:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "SELF_MAINTENANCE_UNAVAILABLE",
                    "message": "PERMISSION_DENIED",
                }
            try:
                report = await service.run_once()
            except Exception as error:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "SELF_MAINTENANCE_FAILED",
                    "message": f"{type(error).__name__}: {error}",
                }
            return f"{command}_result", report

        # ─── Third-party management commands ───────────────────────────
        if command == "app:get-third-party-status":
            sovereign = self._get_third_party_sovereign()
            if sovereign is None:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "THIRD_PARTY_SOVEREIGN_UNAVAILABLE",
                    "message": "PERMISSION_DENIED",
                }
            return f"{command}_result", {
                "ok": True,
                "status": sovereign.get_manager_status(),
                "live_status": sovereign.live_status(),
            }

        if command == "app:probe-third-party-versions":
            sovereign = self._get_third_party_sovereign()
            if sovereign is None:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "THIRD_PARTY_SOVEREIGN_UNAVAILABLE",
                    "message": "PERMISSION_DENIED",
                }
            try:
                versions = sovereign.probe_all_versions()
            except Exception as error:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "PROBE_FAILED",
                    "message": f"{type(error).__name__}: {error}",
                }
            return f"{command}_result", {
                "ok": True,
                "versions": {tid: info.as_dict() for tid, info in versions.items()},
            }

        if command == "app:check-third-party-updates":
            sovereign = self._get_third_party_sovereign()
            if sovereign is None:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "THIRD_PARTY_SOVEREIGN_UNAVAILABLE",
                    "message": "PERMISSION_DENIED",
                }
            try:
                updates = sovereign.check_all_for_updates()
            except Exception as error:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "UPDATE_CHECK_FAILED",
                    "message": f"{type(error).__name__}: {error}",
                }
            return f"{command}_result", {
                "ok": True,
                "updates": {tid: info.as_dict() for tid, info in updates.items()},
            }

        if command == "app:update-third-party-tool":
            sovereign = self._get_third_party_sovereign()
            if sovereign is None:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "THIRD_PARTY_SOVEREIGN_UNAVAILABLE",
                    "message": "PERMISSION_DENIED",
                }
            tool_id = str(payload.get("tool_id") or "").strip()
            if not tool_id:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "MISSING_TOOL_ID",
                    "message": "tool_id is required",
                }
            approval_token = str(payload.get("approval_token") or "").strip()
            try:
                result = await sovereign.apply_approved_update(
                    tool_id, approval_token=approval_token or None
                )
            except Exception as error:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "UPDATE_EXECUTION_FAILED",
                    "message": f"{type(error).__name__}: {error}",
                }
            return f"{command}_result", result.as_dict()

        if command == "app:auto-update-third-party-tools":
            sovereign = self._get_third_party_sovereign()
            if sovereign is None:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "THIRD_PARTY_SOVEREIGN_UNAVAILABLE",
                    "message": "PERMISSION_DENIED",
                }
            approval_token = str(payload.get("approval_token") or "").strip()
            only_available = payload.get("only_available", True) is not False
            if not approval_token:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "APPROVAL_REQUIRED",
                    "message": "governance approval token required for auto-update",
                }
            try:
                results = await sovereign.apply_approved_auto_updates(
                    approval_token=approval_token, only_available=only_available
                )
            except Exception as error:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "AUTO_UPDATE_FAILED",
                    "message": f"{type(error).__name__}: {error}",
                }
            return f"{command}_result", {
                "ok": True,
                "results": {tid: r.as_dict() for tid, r in results.items()},
            }

        # A152: repair coordination status — lets the frontend check whether
        # a repair is already in progress before triggering its own restart,
        # preventing duplicate repair owners.
        if command == "app:get-repair-status":
            from tasks.repair_coordinator import get_repair_coordinator

            coordinator = get_repair_coordinator()
            if coordinator is None:
                return f"{command}_result", {
                    "ok": True,
                    "repair_in_progress": False,
                    "coordinator_initialized": False,
                }
            return f"{command}_result", {
                "ok": True,
                "coordinator_initialized": True,
                **coordinator.get_status(),
            }

        # app:hot-reload-backend — governed system-wide hot-reload trigger.
        # Per E127 (RUNTIME-ACTION:system-runtime), hot-reload is a runtime
        # action owned by the runtime sub-sovereign (under the
        # system-decision-sovereign).  An approval token (capability
        # hot-update/hot-reload) minted through the governance authorization
        # path is required.
        if command == "app:hot-reload-backend":
            system_sovereign = getattr(self.app, "system_sovereign_service", None)
            runtime_sovereign = (
                getattr(system_sovereign, "runtime_sovereign", None)
                if system_sovereign is not None
                else None
            )
            if runtime_sovereign is None:
                return f"{command}_result", {
                    "ok": False,
                    "duty": "runtime-action",
                    "error_code": "RUNTIME_SOVEREIGN_UNAVAILABLE",
                    "message": "PERMISSION_DENIED",
                }
            approval_token = str(payload.get("approval_token") or "").strip()
            modules = payload.get("modules")
            if modules is not None and (
                isinstance(modules, (str, bytes))
                or not isinstance(modules, (list, tuple))
            ):
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "INVALID_MODULES",
                    "message": "modules must be a list of dotted module names",
                }
            try:
                result = await runtime_sovereign.execute_hot_reload(
                    approval_token=approval_token or None,
                    modules=modules,
                )
            except Exception as error:
                return f"{command}_result", {
                    "ok": False,
                    "error_code": "HOT_RELOAD_FAILED",
                    "message": f"{type(error).__name__}: {error}",
                }
            return f"{command}_result", result

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
