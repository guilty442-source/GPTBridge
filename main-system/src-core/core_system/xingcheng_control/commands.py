"""Xingcheng Control Surface — Command Handlers (A366, A380).

Handles the xingcheng-set-repair-release, xingcheng-set-update-release,
xingcheng-confirm-automatic-repair, xingcheng-confirm-automatic-update,
xingcheng-revoke-automatic-repair-confirmation, xingcheng-revoke-automatic-update-confirmation,
sync-execute-approved-automatic-repair, sync-execute-approved-automatic-update commands.
"""

from __future__ import annotations

from typing import Any, Dict

from .surface import (
    XingchengControlSurface,
    REPAIR_RELEASE_SWITCH,
    UPDATE_RELEASE_SWITCH,
    SwitchState,
)

# Command constants (matching command_router constants)
XINGCHENG_SET_REPAIR_RELEASE = "xingcheng-set-repair-release"
XINGCHENG_SET_UPDATE_RELEASE = "xingcheng-set-update-release"
XINGCHENG_CONFIRM_AUTOMATIC_REPAIR = "xingcheng-confirm-automatic-repair"
XINGCHENG_CONFIRM_AUTOMATIC_UPDATE = "xingcheng-confirm-automatic-update"
XINGCHENG_REVOKE_AUTOMATIC_REPAIR = "xingcheng-revoke-automatic-repair-confirmation"
XINGCHENG_REVOKE_AUTOMATIC_UPDATE = "xingcheng-revoke-automatic-update-confirmation"
SYNC_EXECUTE_APPROVED_REPAIR = "sync-execute-approved-automatic-repair"
SYNC_EXECUTE_APPROVED_UPDATE = "sync-execute-approved-automatic-update"


class XingchengCommandHandler:
    """Command handler for Xingcheng Control Surface commands."""

    def __init__(self, control_surface: XingchengControlSurface) -> None:
        self.control_surface = control_surface

    async def handle(self, command: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Handle a Xingcheng command."""
        handlers = {
            XINGCHENG_SET_REPAIR_RELEASE: self._handle_set_repair_release,
            XINGCHENG_SET_UPDATE_RELEASE: self._handle_set_update_release,
            XINGCHENG_CONFIRM_AUTOMATIC_REPAIR: self._handle_confirm_automatic_repair,
            XINGCHENG_CONFIRM_AUTOMATIC_UPDATE: self._handle_confirm_automatic_update,
            XINGCHENG_REVOKE_AUTOMATIC_REPAIR: self._handle_revoke_automatic_repair,
            XINGCHENG_REVOKE_AUTOMATIC_UPDATE: self._handle_revoke_automatic_update,
            SYNC_EXECUTE_APPROVED_REPAIR: self._handle_sync_execute_repair,
            SYNC_EXECUTE_APPROVED_UPDATE: self._handle_sync_execute_update,
        }

        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {
                "ok": False,
                "error_code": "UNKNOWN_COMMAND",
                "message": f"Unknown Xingcheng command: {command}",
            }

        return await handler(payload)

    async def _handle_set_repair_release(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Handle xingcheng-set-repair-release."""
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return "xingcheng-set-repair-release_result", {
                "ok": False,
                "error_code": "MISSING_ENABLED_STATE",
                "message": "enabled (boolean) is required",
            }

        state = SwitchState.ENABLED if enabled else SwitchState.DISABLED
        status = self.control_surface.set_switch("xingcheng.repair_release", state, "authenticated-ui")
        return "xingcheng-set-repair-release_result", {"ok": True, "switches": self._get_all_switches_status()}

    async def _handle_set_update_release(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Handle xingcheng-set-update-release."""
        enabled = payload.get("enabled")
        if not isinstance(enabled, bool):
            return "xingcheng-set-update-release_result", {
                "ok": False,
                "error_code": "MISSING_ENABLED_STATE",
                "message": "enabled (boolean) is required",
            }

        state = SwitchState.ENABLED if enabled else SwitchState.DISABLED
        status = self.control_surface.set_switch("xingcheng.update_release", state, "authenticated-ui")
        return "xingcheng-set-update-release_result", {"ok": True, "switches": self._get_all_switches_status()}

    async def _handle_confirm_automatic_repair(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Handle xingcheng-confirm-automatic-repair."""
        from core_system.confirmation_service import record_confirmation

        result = await record_confirmation(
            self.control_surface.app,
            str(payload.get("action_id") or ""),
            confirmation_id=str(payload.get("confirmation_id") or ""),
        )
        result.setdefault("error_code", "")
        return "xingcheng-confirm-automatic-repair_result", result

    async def _handle_confirm_automatic_update(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Handle xingcheng-confirm-automatic-update."""
        from core_system.confirmation_service import record_confirmation

        result = await record_confirmation(
            self.control_surface.app,
            str(payload.get("action_id") or ""),
            confirmation_id=str(payload.get("confirmation_id") or ""),
        )
        result.setdefault("error_code", "")
        return "xingcheng-confirm-automatic-update_result", result

    async def _handle_revoke_automatic_repair(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Handle xingcheng-revoke-automatic-repair-confirmation."""
        from core_system.confirmation_service import revoke_confirmation

        result = await revoke_confirmation(
            self.control_surface.app,
            str(payload.get("action_id") or ""),
            str(payload.get("confirmation_id") or ""),
        )
        result.setdefault("error_code", "")
        return "xingcheng-revoke-automatic-repair-confirmation_result", result

    async def _handle_revoke_automatic_update(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Handle xingcheng-revoke-automatic-update-confirmation."""
        from core_system.confirmation_service import revoke_confirmation

        result = await revoke_confirmation(
            self.control_surface.app,
            str(payload.get("action_id") or ""),
            str(payload.get("confirmation_id") or ""),
        )
        result.setdefault("error_code", "")
        return "xingcheng-revoke-automatic-update-confirmation_result", result

    async def _handle_sync_execute_repair(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Handle sync-execute-approved-automatic-repair."""
        from core_system.confirmation_service import execute_approved

        result = await execute_approved(
            self.control_surface.app,
            str(payload.get("action_id") or ""),
            str(payload.get("confirmation_id") or ""),
        )
        result.setdefault("error_code", "")
        return "sync-execute-approved-automatic-repair_result", result

    async def _handle_sync_execute_update(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        """Handle sync-execute-approved-automatic-update."""
        from core_system.confirmation_service import execute_approved

        result = await execute_approved(
            self.control_surface.app,
            str(payload.get("action_id") or ""),
            str(payload.get("confirmation_id") or ""),
        )
        result.setdefault("error_code", "")
        return "sync-execute-approved-automatic-update_result", result

    def _get_all_switches_status(self) -> Dict[str, Any]:
        """Get status of all switches for response."""
        repair = self.control_surface.store.get_switch("xingcheng.repair_release")
        update = self.control_surface.store.get_switch("xingcheng.update_release")
        return {
            "repair_release": {
                "enabled": repair.state.value == "enabled",
                "state": repair.state.value,
                "enabled_at": repair.enabled_at,
                "disabled_at": repair.disabled_at,
                "enabled_by": repair.enabled_by,
            },
            "update_release": {
                "enabled": update.state.value == "enabled",
                "state": update.state.value,
                "enabled_at": update.enabled_at,
                "disabled_at": update.disabled_at,
                "enabled_by": update.enabled_by,
            },
        }


__all__ = ["XingchengCommandHandler"]