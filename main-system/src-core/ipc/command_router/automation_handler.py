"""Command Router — Automation Switches and Pending Actions Handlers."""

from __future__ import annotations

from typing import Any, Dict


class AutomationSwitchesHandler:
    """Handle automation switches and pending actions commands."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def handle(self, command: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        handlers = {
            "app:get-pending-actions": self._handle_get_pending_actions,
            "app:get-automation-switches": self._handle_get_switches,
            "app:set-automation-switch": self._handle_set_switch,
        }
        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {
                "ok": False,
                "error_code": "UNKNOWN_COMMAND",
                "message": "Unknown automation command",
            }
        return await handler(payload)

    async def _handle_get_pending_actions(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from core_system.auto_action_policy import (
            read_actionable_pending_actions,
            read_automation_switches,
        )
        from .constants import _pending_action_cardinality

        project_root = getattr(self.app, "project_root", None)
        actions = (
            read_actionable_pending_actions(project_root)
            if project_root
            else []
        )
        return "app:get-pending-actions_result", {
            "ok": True,
            "actions": actions,
            "switches": read_automation_switches(project_root),
            "cardinality": _pending_action_cardinality(actions),
        }

    async def _handle_get_switches(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from core_system.auto_action_policy import read_automation_switches

        project_root = getattr(self.app, "project_root", None)
        return "app:get-automation-switches_result", {
            "ok": True,
            "switches": read_automation_switches(project_root),
        }

    async def _handle_set_switch(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from core_system.auto_action_policy import (
            read_automation_switches,
            set_automation_switch,
        )

        switch = str(payload.get("switch") or "").strip()
        if payload.get("enabled") is None:
            return "app:set-automation-switch_result", {
                "ok": False,
                "error_code": "MISSING_ENABLED_STATE",
                "message": "enabled (boolean) is required",
            }
        try:
            switches = set_automation_switch(
                getattr(self.app, "project_root", None),
                switch,
                bool(payload.get("enabled")),
                actor="authenticated-ui",
            )
        except ValueError as error:
            return "app:set-automation-switch_result", {
                "ok": False,
                "error_code": "UNKNOWN_SWITCH",
                "message": str(error),
                "switches": read_automation_switches(
                    getattr(self.app, "project_root", None)
                ),
            }
        return "app:set-automation-switch_result", {
            "ok": True,
            "switches": switches,
        }