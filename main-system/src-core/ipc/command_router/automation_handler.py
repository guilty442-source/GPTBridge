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
            "app:get-resource-mode": self._handle_get_resource_mode,
            "app:set-resource-mode": self._handle_set_resource_mode,
            "app:propose-system-modification": self._handle_propose_system_modification,
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
                "error_code": (
                    "SWITCH_RETIRED"
                    if switch == "automatic_repair_enabled"
                    else "UNKNOWN_SWITCH"
                ),
                "message": str(error),
                "switches": read_automation_switches(
                    getattr(self.app, "project_root", None)
                ),
            }
        return "app:set-automation-switch_result", {
            "ok": True,
            "switches": switches,
        }

    async def _handle_get_resource_mode(
        self, payload: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        from tasks.resource_governor_signal import governor_mode

        return "app:get-resource-mode_result", {
            "ok": True,
            "resource_mode": governor_mode(),
        }

    async def _handle_set_resource_mode(
        self, payload: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        from tasks.resource_governor_signal import (
            governor_mode,
            set_governor_auto,
            set_governor_mode,
        )

        mode = str(payload.get("mode") or "").strip()
        if not mode:
            return "app:set-resource-mode_result", {
                "ok": False,
                "error_code": "MISSING_MODE",
                "message": "mode (low / medium / high / auto) is required",
            }
        try:
            if mode == "auto":
                status = set_governor_auto(True, actor="authenticated-ui")
            else:
                status = set_governor_mode(mode, actor="authenticated-ui")
        except ValueError as error:
            return "app:set-resource-mode_result", {
                "ok": False,
                "error_code": "MODE_UNKNOWN",
                "message": str(error),
                "resource_mode": governor_mode(),
            }
        except OSError as error:
            return "app:set-resource-mode_result", {
                "ok": False,
                "error_code": "RULES_WRITE_FAILED",
                "message": str(error),
                "resource_mode": governor_mode(),
            }
        return "app:set-resource-mode_result", {
            "ok": True,
            "resource_mode": status,
        }

    async def _handle_propose_system_modification(
        self, payload: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        """Queue a model-dialogue-issued system change for single-item
        permission (P21).

        The proposal becomes an A366 pending action of kind
        ``system-modification``.  That kind has no standing switch —
        execution is possible only after a bound, expiring, single-use
        confirmation.  ``detail.operation`` selects the bounded executor
        (config_value / codex_amendment_request / rollback_of).
        """
        from core_system.auto_action_policy import (
            SYSTEM_MODIFICATION_KIND,
            record_pending_action,
        )

        project_root = getattr(self.app, "project_root", None)
        if project_root is None:
            return "app:propose-system-modification_result", {
                "ok": False,
                "error_code": "PROJECT_ROOT_UNAVAILABLE",
                "message": "project root is unavailable",
            }
        summary = str(payload.get("summary") or "").strip()
        detail = payload.get("detail")
        if not summary or not isinstance(detail, dict):
            return "app:propose-system-modification_result", {
                "ok": False,
                "error_code": "MISSING_FIELDS",
                "message": "summary (str) and detail (object) are required",
            }
        operation = str(detail.get("operation") or "")
        if operation not in (
            "config_value",
            "codex_amendment_request",
            "rollback_of",
        ):
            return "app:propose-system-modification_result", {
                "ok": False,
                "error_code": "OPERATION_UNKNOWN",
                "message": (
                    "detail.operation must be one of config_value, "
                    "codex_amendment_request, rollback_of"
                ),
            }
        expires_at = str(payload.get("expires_at") or "").strip()
        if not expires_at:
            return "app:propose-system-modification_result", {
                "ok": False,
                "error_code": "EXPIRES_AT_REQUIRED",
                "message": (
                    "expires_at is required — system-modification "
                    "proposals must carry a confirmation deadline"
                ),
            }
        record = record_pending_action(
            project_root,
            kind=SYSTEM_MODIFICATION_KIND,
            summary=summary,
            detail=detail,
            binding={
                "scope": str(payload.get("scope") or ""),
                "target": str(payload.get("target") or ""),
                "proposed_method": str(
                    payload.get("proposed_method") or summary
                ),
                "risk": str(payload.get("risk") or ""),
                "rollback": str(payload.get("rollback") or ""),
                "expires_at": expires_at,
            },
        )
        return "app:propose-system-modification_result", {
            "ok": True,
            "action_id": record.get("action_id"),
            "status": record.get("status"),
            "evidence_digest": record.get("evidence_digest"),
            "permission": "single-item only (no standing switch)",
        }