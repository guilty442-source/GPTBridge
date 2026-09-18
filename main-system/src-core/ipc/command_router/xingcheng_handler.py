"""Command Router — Xingcheng Confirmation Handlers."""

from __future__ import annotations

from typing import Any, Dict


class XingchengConfirmationHandler:
    """Handle Xingcheng confirmation and sync-execute commands."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def handle(self, command: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        handlers = {
            "xingcheng-set-repair-release": lambda p: self._handle_set_release("repair", p),
            "xingcheng-set-update-release": lambda p: self._handle_set_release("update", p),
            "xingcheng-set-native-model-enabled": self._handle_native_model_enabled,
            "xingcheng-confirm-automatic-repair": lambda p: self._handle_confirmation(command, p),
            "xingcheng-confirm-automatic-update": lambda p: self._handle_confirmation(command, p),
            "xingcheng-deny-pending-action": self._handle_deny,
            "xingcheng-revoke-automatic-repair-confirmation": lambda p: self._handle_revoke(command, p),
            "xingcheng-revoke-automatic-update-confirmation": lambda p: self._handle_revoke(command, p),
            "sync-execute-approved-automatic-repair": lambda p: self._handle_sync_execute(command, p),
            "sync-execute-approved-automatic-update": lambda p: self._handle_sync_execute(command, p),
        }
        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {
                "ok": False,
                "error_code": "UNKNOWN_COMMAND",
                "message": "Unknown Xingcheng command",
            }
        return await handler(payload)

    async def _handle_native_model_enabled(
        self, payload: Dict[str, Any]
    ) -> tuple[str, Dict[str, Any]]:
        from core_system.xingcheng_native_model_runtime import (
            set_native_model_enabled,
        )

        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return "xingcheng-set-native-model-enabled_result", {
                "ok": False,
                "error_code": "MISSING_ENABLED_STATE",
                "message": "enabled (boolean) is required",
            }
        result = await set_native_model_enabled(enabled)
        return "xingcheng-set-native-model-enabled_result", result

    async def _handle_set_release(self, kind: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from core_system.auto_action_policy import (
            AUTOMATIC_REPAIR_SWITCH,
            AUTOMATIC_UPDATE_SWITCH,
            set_automation_switch,
        )

        switch = AUTOMATIC_REPAIR_SWITCH if kind == "repair" else AUTOMATIC_UPDATE_SWITCH
        enabled = payload.get("enabled")
        event_name = f"xingcheng-set-{kind}-release_result"
        if not isinstance(enabled, bool):
            return event_name, {
                "ok": False,
                "error_code": "MISSING_ENABLED_STATE",
                "message": "enabled (boolean) is required",
            }
        if kind == "repair":
            # Retired: autonomous repair runs under the system-audit flow.
            return event_name, {
                "ok": False,
                "error_code": "SWITCH_RETIRED",
                "message": (
                    "repair_release was retired: autonomous repair runs "
                    "under the system-audit flow"
                ),
            }
        switches = set_automation_switch(
            getattr(self.app, "project_root", None),
            switch,
            enabled,
            actor="authenticated-ui",
        )
        return event_name, {"ok": True, "switches": switches}

    async def _handle_confirmation(self, command: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from core_system.confirmation_service import record_confirmation

        result = await record_confirmation(
            self.app,
            str(payload.get("action_id") or ""),
            confirmation_id=str(payload.get("confirmation_id") or ""),
            permission_mode=str(payload.get("permission_mode") or "standing-switch"),
        )
        result.setdefault("error_code", "")
        return f"{command}_result", result

    async def _handle_revoke(self, command: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from core_system.confirmation_service import revoke_confirmation

        result = await revoke_confirmation(
            self.app,
            str(payload.get("action_id") or ""),
            str(payload.get("confirmation_id") or ""),
        )
        result.setdefault("error_code", "")
        return f"{command}_result", result

    async def _handle_sync_execute(self, command: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from core_system.confirmation_service import execute_approved

        result = await execute_approved(
            self.app,
            str(payload.get("action_id") or ""),
            str(payload.get("confirmation_id") or ""),
        )
        result.setdefault("error_code", "")
        return f"{command}_result", result

    async def _handle_deny(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from core_system.confirmation_service import deny_pending_action

        result = await deny_pending_action(
            self.app, str(payload.get("action_id") or "")
        )
        result.setdefault("error_code", "")
        return "xingcheng-deny-pending-action_result", result
