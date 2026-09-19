"""Command Router — Main entry point for IPC command routing."""

from __future__ import annotations

import sys
from typing import Any, Dict, TYPE_CHECKING

if TYPE_CHECKING:
    from main import GPTBridgeApp

from .constants import (
    MAIN_COMMANDS,
    TOOL_LIFECYCLE_HANDLERS,
)
from .governance_handler import GovernanceRulesHandler
from .third_party_handler import ThirdPartyHandler
from .repair_handler import RepairMaintenanceHandler
from .hot_reload_handler import HotReloadHandler
from .fault_analysis_handler import FaultAnalysisHandler
from .saga_handler import SagaOperationsHandler
from .automation_handler import AutomationSwitchesHandler
from .xingcheng_handler import XingchengConfirmationHandler
from .tool_lifecycle_handler import ToolLifecycleHandler


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

        # Initialize sub-handlers
        self._governance_handler = GovernanceRulesHandler(app)
        self._third_party_handler = ThirdPartyHandler(app)
        self._repair_handler = RepairMaintenanceHandler(app)
        self._hot_reload_handler = HotReloadHandler(app)
        self._fault_analysis_handler = FaultAnalysisHandler(app)
        self._saga_handler = SagaOperationsHandler(app)
        self._automation_handler = AutomationSwitchesHandler(app)
        self._xingcheng_handler = XingchengConfirmationHandler(app)
        self._tool_lifecycle_handler = ToolLifecycleHandler(app, toolbox_service)

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

        # Governance rules
        if command == "app:get-governance-rules":
            return await self._governance_handler.handle(payload)

        # Third-party management
        if command in (
            "app:get-third-party-status",
            "app:probe-third-party-versions",
            "app:check-third-party-updates",
            "app:update-third-party-tool",
            "app:auto-update-third-party-tools",
        ):
            return await self._third_party_handler.handle(command, payload)

        # Repair coordination
        if command in (
            "app:get-repair-status",
            "app:run-main-system-self-maintenance",
        ):
            return await self._repair_handler.handle(command, payload)

        # Hot-reload (A330)
        if command == "app:hot-reload-backend":
            return await self._hot_reload_handler.handle(payload)

        # Fault analysis
        if command == "app:get-fault-analysis":
            return await self._fault_analysis_handler.handle(payload)

        # Saga operations (read-only diagnostics)
        if command in ("app:get-saga-operations", "app:get-saga-operation"):
            return await self._saga_handler.handle(command, payload)

        # Automation switches and pending actions
        if command in (
            "app:get-pending-actions",
            "app:get-automation-switches",
            "app:set-automation-switch",
        ):
            return await self._automation_handler.handle(command, payload)

        # Xingcheng commands
        if command in (
            "xingcheng-set-repair-release",
            "xingcheng-set-update-release",
            "xingcheng-set-native-model-enabled",
            "xingcheng-confirm-automatic-repair",
            "xingcheng-confirm-automatic-update",
            "xingcheng-deny-pending-action",
            "xingcheng-revoke-automatic-repair-confirmation",
            "xingcheng-revoke-automatic-update-confirmation",
            "sync-execute-approved-automatic-repair",
            "sync-execute-approved-automatic-update",
        ):
            return await self._xingcheng_handler.handle(command, payload)

        # Tool lifecycle
        if command in TOOL_LIFECYCLE_HANDLERS:
            return await self._tool_lifecycle_handler.handle(command, payload)

        # Runtime status service
        if self.runtime_status_service and self.runtime_status_service.owns(command):
            return await self.runtime_status_service.handle(command, payload)

        return f"{command}_result", {
            "ok": False,
            "message": f"Command '{command}' has no available handler.",
        }


__all__ = [
    "CommandRouter",
    "MAIN_COMMANDS",
    "TOOL_LIFECYCLE_HANDLERS",
]
