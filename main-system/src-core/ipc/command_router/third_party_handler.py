"""Command Router — Third-Party Management Handlers."""

from __future__ import annotations

from typing import Any, Dict


class ThirdPartyHandler:
    """Handle third-party management commands."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def _get_third_party_sovereign(self) -> Any:
        decision_sovereign = getattr(self.app, "decision_sovereign", None)
        if decision_sovereign is None:
            return None
        return getattr(decision_sovereign, "third_party_sovereign", None)

    async def handle(self, command: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        handlers = {
            "app:get-third-party-status": self._handle_get_status,
            "app:probe-third-party-versions": self._handle_probe_versions,
            "app:check-third-party-updates": self._handle_check_updates,
            "app:update-third-party-tool": self._handle_update_tool,
            "app:auto-update-third-party-tools": self._handle_auto_update,
        }
        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {
                "ok": False,
                "error_code": "UNKNOWN_COMMAND",
                "message": "Unknown third-party command",
            }
        return await handler(payload)

    async def _handle_get_status(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        sovereign = self._get_third_party_sovereign()
        if sovereign is None:
            return "app:get-third-party-status_result", {
                "ok": False,
                "error_code": "THIRD_PARTY_SOVEREIGN_UNAVAILABLE",
                "message": "PERMISSION_DENIED",
            }
        return "app:get-third-party-status_result", {
            "ok": True,
            "status": sovereign.get_manager_status(),
            "live_status": sovereign.live_status(),
        }

    async def _handle_probe_versions(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        sovereign = self._get_third_party_sovereign()
        if sovereign is None:
            return "app:probe-third-party-versions_result", {
                "ok": False,
                "error_code": "THIRD_PARTY_SOVEREIGN_UNAVAILABLE",
                "message": "PERMISSION_DENIED",
            }
        try:
            versions = sovereign.probe_all_versions()
        except Exception as error:
            return "app:probe-third-party-versions_result", {
                "ok": False,
                "error_code": "PROBE_FAILED",
                "message": f"{type(error).__name__}: {error}",
            }
        return "app:probe-third-party-versions_result", {
            "ok": True,
            "versions": {tid: info.as_dict() for tid, info in versions.items()},
        }

    async def _handle_check_updates(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        sovereign = self._get_third_party_sovereign()
        if sovereign is None:
            return "app:check-third-party-updates_result", {
                "ok": False,
                "error_code": "THIRD_PARTY_SOVEREIGN_UNAVAILABLE",
                "message": "PERMISSION_DENIED",
            }
        try:
            updates = sovereign.check_all_for_updates()
        except Exception as error:
            return "app:check-third-party-updates_result", {
                "ok": False,
                "error_code": "UPDATE_CHECK_FAILED",
                "message": f"{type(error).__name__}: {error}",
            }
        return "app:check-third-party-updates_result", {
            "ok": True,
            "updates": {tid: info.as_dict() for tid, info in updates.items()},
        }

    async def _handle_update_tool(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        sovereign = self._get_third_party_sovereign()
        if sovereign is None:
            return "app:update-third-party-tool_result", {
                "ok": False,
                "error_code": "THIRD_PARTY_SOVEREIGN_UNAVAILABLE",
                "message": "PERMISSION_DENIED",
            }
        tool_id = str(payload.get("tool_id") or "").strip()
        if not tool_id:
            return "app:update-third-party-tool_result", {
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
            return "app:update-third-party-tool_result", {
                "ok": False,
                "error_code": "UPDATE_EXECUTION_FAILED",
                "message": f"{type(error).__name__}: {error}",
            }
        return "app:update-third-party-tool_result", result.as_dict()

    async def _handle_auto_update(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        sovereign = self._get_third_party_sovereign()
        if sovereign is None:
            return "app:auto-update-third-party-tools_result", {
                "ok": False,
                "error_code": "THIRD_PARTY_SOVEREIGN_UNAVAILABLE",
                "message": "PERMISSION_DENIED",
            }
        approval_token = str(payload.get("approval_token") or "").strip()
        only_available = payload.get("only_available", True) is not False
        if not approval_token:
            return "app:auto-update-third-party-tools_result", {
                "ok": False,
                "error_code": "APPROVAL_REQUIRED",
                "message": "governance approval token required for auto-update",
            }
        try:
            results = await sovereign.apply_approved_auto_updates(
                approval_token=approval_token, only_available=only_available
            )
        except Exception as error:
            return "app:auto-update-third-party-tools_result", {
                "ok": False,
                "error_code": "AUTO_UPDATE_FAILED",
                "message": f"{type(error).__name__}: {error}",
            }
        return "app:auto-update-third-party-tools_result", {
            "ok": True,
            "results": {tid: r.as_dict() for tid, r in results.items()},
        }