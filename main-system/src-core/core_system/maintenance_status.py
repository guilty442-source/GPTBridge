"""Maintenance Sovereign — status reporting and health monitoring mixin.

Extracted from ``maintenance_sovereign`` to keep each module focused and
under 500 lines.
"""

from __future__ import annotations

from typing import Any

from .codex_decision import decision_basis
from .native import native_available, resource_status


class MaintenanceStatusMixin:
    """Status reporting and health-monitoring methods.

    Expects the following attributes to be set by the composing class's
    ``__init__``:

      * ``self.app`` — the GPTBridgeApp instance
      * ``self._started`` / ``self._started_at`` / ``self._stopped_at``
      * ``self._daily_cleaner`` / ``self._repair_service``
      * ``self._health_checker``
      * ``self._capability_task`` / ``self._capability_interval_seconds``
      * ``self.ROLE``
    """

    # ------------------------------------------------------------------
    # Maintenance status
    # ------------------------------------------------------------------

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "all-system-maintenance-functions",
            "responsibilities": list(self._maintenance_responsibilities()),
            "started": self._started,
            "maintenance_ready": bool(getattr(self.app, "maintenance_ready", False)),
            "update": self._update_status(),
            "health_monitoring": self._health_monitoring(),
            "automatic_repair": self._automatic_repair_status(),
            "fault_determination": self._fault_determination_status(),
            "backup": self._backup_status(),
            "daily_cleaner": self._daily_cleaner_status(),
            "module_cleanup": self._module_cleanup_status(),
            "main_system_self_maintenance": self._main_system_self_maintenance_status(),
            "learning": self.learning_status(),
            "repair_decision_chain": self._repair_decision_chain_status(),
            "capability_loop": {
                "running": self._capability_task is not None and not self._capability_task.done(),
                "interval_seconds": self._capability_interval_seconds,
            },
            "native": resource_status(),
            "decision": decision_basis(self._maintenance_area()),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "all-system-maintenance-functions",
            "responsibilities": list(self._maintenance_responsibilities()),
            "update": self._update_status(),
            "health_monitoring": self._health_monitoring(),
            "automatic_repair": self._automatic_repair_status(),
            "fault_determination": self._fault_determination_status(),
            "backup": self._backup_status(),
            "module_cleanup": self._module_cleanup_status(),
            "main_system_self_maintenance": self._main_system_self_maintenance_status(),
            "learning": self.learning_status(),
            "repair_decision_chain": self._repair_decision_chain_status(),
            "delegation": "governed-executor-only",
            "native_kernel": native_available(),
            "decision": decision_basis(self._maintenance_area()),
        }

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------

    def _health_monitoring(self) -> dict[str, Any]:
        """System-health monitoring (incl. data integrity), decision only.

        The sovereign runs the read-only health checker and surfaces the
        governance runtime-integrity state; it never performs the heavy repair
        or integrity remediation work in-process (delegated to executors).
        """

        report: dict[str, Any] = {}
        checker = self._health_checker
        if callable(checker):
            try:
                report = checker(getattr(self.app, "project_root", None))
            except Exception:
                report = {"error": "health-checker-unavailable"}
        self.app.health_snapshot = report

        governance = getattr(self.app, "governance", None)
        integrity_ready: bool | None = None
        if governance is not None and hasattr(governance, "runtime_integrity_ready"):
            try:
                integrity_ready = bool(governance.runtime_integrity_ready())
            except Exception:
                integrity_ready = None
        self.app.governance_integrity_ready = integrity_ready

        return {
            "monitoring": "system-health",
            "includes": ["runtime", "resource", "data-integrity"],
            "report": report,
            "capability_check": self._capability_status(),
            "governance_integrity_ready": integrity_ready,
            "decision": decision_basis(self._maintenance_area())["edicts"],
        }

    # ------------------------------------------------------------------
    # Internals — daily cleaner / module cleanup / self-maintenance status
    # ------------------------------------------------------------------

    def _daily_cleaner_status(self) -> dict[str, Any]:
        if self._daily_cleaner is None:
            return {"enabled": False}
        get_status = getattr(self._daily_cleaner, "status", None)
        if callable(get_status):
            return get_status()
        return {"enabled": True}

    def executor_ownership_status(self) -> dict[str, Any]:
        """Expose executor availability without leaking executor references."""
        return {
            "owner": self.ROLE,
            "daily_global_cleaner": self._daily_cleaner is not None,
            "central_repair": self._repair_service is not None,
        }

    def _module_cleanup_status(self) -> dict[str, Any]:
        """Unified oversight of devolved per-module self-cleanup.

        Execution remains with each module's own local cleanup; the sovereign
        surfaces the aggregated daily sweep collected by the scheduler.
        """

        if self._daily_cleaner is None:
            return {
                "enabled": False,
                "authority": self.ROLE,
                "execution": "devolved-per-module",
            }
        get_status = getattr(self._daily_cleaner, "module_cleanup_status", None)
        if callable(get_status):
            try:
                report = get_status()
            except Exception:
                report = {}
            return {
                "enabled": True,
                "authority": self.ROLE,
                "execution": "devolved-per-module",
                "schedule": "daily-governed-maintenance",
                "last_sweep": report or None,
            }
        return {
            "enabled": True,
            "authority": self.ROLE,
            "execution": "devolved-per-module",
        }

    def _main_system_self_maintenance_status(self) -> dict[str, Any]:
        """Oversight of the main system's own self-maintenance loop.

        The sovereign supervises; execution stays with the bounded
        ``MainSystemSelfMaintenance`` governed executor.
        """

        service = getattr(self.app, "main_system_self_maintenance", None)
        if service is None:
            return {
                "enabled": False,
                "authority": self.ROLE,
                "execution": "governed-executor-only",
            }
        try:
            report = service.status()
        except Exception:
            report = {"enabled": True, "available": False}
        return {
            "enabled": True,
            "authority": self.ROLE,
            "execution": "governed-executor-only",
            "schedule": "startup-plus-periodic-plus-manual",
            "service": report,
        }

    # ------------------------------------------------------------------
    # Helpers for accessing module-level constants without circular imports
    # ------------------------------------------------------------------

    def _maintenance_responsibilities(self):
        from .maintenance_sovereign import MAINTENANCE_RESPONSIBILITIES
        return MAINTENANCE_RESPONSIBILITIES

    def _maintenance_area(self):
        from .maintenance_sovereign import _MAINTENANCE_SOVEREIGN
        return _MAINTENANCE_SOVEREIGN.area
