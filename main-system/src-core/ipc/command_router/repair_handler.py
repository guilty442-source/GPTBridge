"""Command Router — Repair and Self-Maintenance Handlers."""

from __future__ import annotations

from typing import Any, Dict


class RepairMaintenanceHandler:
    """Handle repair coordination and self-maintenance commands."""

    def __init__(self, app: Any) -> None:
        self.app = app

    def _get_maintenance_sovereign(self) -> Any:
        return getattr(self.app, "maintenance_sovereign", None)

    async def handle(self, command: str, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        handlers = {
            "app:get-repair-status": self._handle_repair_status,
            "app:run-main-system-self-maintenance": self._handle_self_maintenance,
        }
        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {
                "ok": False,
                "error_code": "UNKNOWN_COMMAND",
                "message": "Unknown repair command",
            }
        return await handler(payload)

    async def _handle_repair_status(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        from tasks.repair_coordinator import get_repair_coordinator

        coordinator = get_repair_coordinator()
        if coordinator is None:
            return "app:get-repair-status_result", {
                "ok": True,
                "repair_in_progress": False,
                "coordinator_initialized": False,
            }
        return "app:get-repair-status_result", {
            "ok": True,
            "coordinator_initialized": True,
            **coordinator.get_status(),
        }

    async def _handle_self_maintenance(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        service = getattr(self.app, "main_system_self_maintenance", None)
        if service is None:
            return "app:run-main-system-self-maintenance_result", {
                "ok": False,
                "error_code": "SELF_MAINTENANCE_UNAVAILABLE",
                "message": "PERMISSION_DENIED",
            }
        try:
            report = await service.run_once()
        except Exception as error:
            return "app:run-main-system-self-maintenance_result", {
                "ok": False,
                "error_code": "SELF_MAINTENANCE_FAILED",
                "message": f"{type(error).__name__}: {error}",
            }
        return "app:run-main-system-self-maintenance_result", report