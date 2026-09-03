"""Maintenance Sovereign — owns ALL system-maintenance-related functionality.

The Maintenance Sovereign is responsible for every system-maintenance concern
per the Governance Codex:

  * update                — system / module updates
  * system health         — system health monitoring (including data integrity)
  * automatic repair      — system automatic repair
  * fault determination   — system fault / failure determination
  * backup                — backup coordination

It is the sole owner of system-maintenance matters; no module or other
authority may take over maintenance concerns.  It is LOCAL CODE (same process
as GPTBridgeApp) that coordinates existing in-process services (injected as
references) and delegates heavy execution to governed executors; it never runs
that heavy work in the mother process.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX

from .codex_decision import decision_basis
from .native import (
    monotonic_seconds,
    native_available,
    release_resources,
    resource_status,
)
from core.health import check_core_health


_MAINTENANCE_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "maintenance"),
    None,
)
if _MAINTENANCE_SOVEREIGN is None:
    raise RuntimeError("maintenance sovereign not found in Governance Codex")

MAINTENANCE_RESPONSIBILITIES = _MAINTENANCE_SOVEREIGN.duties


class MaintenanceSovereign:
    """In-process sovereign responsible for ALL system-maintenance functions.

    Responsibilities (any system-maintenance-related function):
      - update
      - system health monitoring (including data integrity)
      - automatic repair
      - fault determination
      - backup
      - (plus periodic/resource maintenance delegated to governed executors)
    """

    ROLE = "maintenance-sovereign"

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._daily_cleaner: Any | None = None
        self._resource_task: asyncio.Task[Any] | None = None
        self._resource_interval_seconds = 300.0
        self._hot_update: Any | None = None
        self._repair_service: Any | None = None
        self._health_checker: Any = check_core_health

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        daily_cleaner: Any = None,
        resource_release: Any = None,
        resource_interval_seconds: float = 300.0,
        hot_update: Any = None,
        repair_service: Any = None,
        health_checker: Any = None,
    ) -> dict[str, Any]:
        """Start the Maintenance Sovereign and its in-process maintenance loops.

        ``daily_cleaner``: the DailyGlobalCleanerService instance already built
            and started by the app (its loop runs independently).
        ``resource_release``: a callable returning a dict (e.g. a memory
            release function); run periodically on a thread if provided.
        ``hot_update``: the governed HotUpdateService; the sovereign supervises
            the frozen, version-gated update boundary (decision only).
        ``repair_service``: the governed CentralRepairService; the sovereign
            surfaces fault-determination, automatic-repair and backup-extraction
            status but never runs the heavy work in-process.
        ``health_checker``: a callable returning a health report dict (default
            ``core.health.check_core_health``), used for system-health monitoring.
        """

        self._daily_cleaner = daily_cleaner
        self.resource_release = resource_release or release_resources
        self._resource_interval_seconds = max(60.0, float(resource_interval_seconds))
        self._hot_update = hot_update
        self._repair_service = repair_service
        if health_checker is not None:
            self._health_checker = health_checker
        self._started_at = self._iso_now()
        self._started = True

        if self._resource_task is None:
            self._resource_task = asyncio.create_task(
                self._resource_loop(),
                name="maintenance-sovereign-resource",
            )

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "responsibilities": list(MAINTENANCE_RESPONSIBILITIES),
            "daily_cleaner": self._daily_cleaner_status(),
        }

    async def stop(self) -> None:
        if self._resource_task is not None:
            self._resource_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._resource_task
            self._resource_task = None
        self._daily_cleaner = None
        self.resource_release = None
        self._hot_update = None
        self._repair_service = None
        self._started = False
        self._stopped_at = self._iso_now()

    # ------------------------------------------------------------------
    # Maintenance status
    # ------------------------------------------------------------------

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "all-system-maintenance-functions",
            "responsibilities": list(MAINTENANCE_RESPONSIBILITIES),
            "started": self._started,
            "maintenance_ready": bool(getattr(self.app, "maintenance_ready", False)),
            "update": self._update_status(),
            "health_monitoring": self._health_monitoring(),
            "automatic_repair": self._automatic_repair_status(),
            "fault_determination": self._fault_determination_status(),
            "backup": self._backup_status(),
            "daily_cleaner": self._daily_cleaner_status(),
            "resource_loop": {
                "running": self._resource_task is not None and not self._resource_task.done(),
                "interval_seconds": self._resource_interval_seconds,
            },
            "native": resource_status(),
            "decision": decision_basis("maintenance"),
            "started_at": self._started_at,
            "stopped_at": self._stopped_at,
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "scope": "all-system-maintenance-functions",
            "responsibilities": list(MAINTENANCE_RESPONSIBILITIES),
            "update": self._update_status(),
            "health_monitoring": self._health_monitoring(),
            "automatic_repair": self._automatic_repair_status(),
            "fault_determination": self._fault_determination_status(),
            "backup": self._backup_status(),
            "state": "running" if self._started else "stopped",
            "delegation": "governed-executor-only",
            "native_kernel": native_available(),
            "decision": decision_basis("maintenance"),
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

        governance = getattr(self.app, "governance", None)
        integrity_ready: bool | None = None
        if governance is not None and hasattr(governance, "runtime_integrity_ready"):
            try:
                integrity_ready = bool(governance.runtime_integrity_ready())
            except Exception:
                integrity_ready = None

        return {
            "monitoring": "system-health",
            "includes": ["runtime", "resource", "data-integrity"],
            "report": report,
            "governance_integrity_ready": integrity_ready,
            "decision": decision_basis("maintenance")["edicts"],
        }

    # ------------------------------------------------------------------
    # Update / automatic-repair / fault-determination / backup surfaces
    # ------------------------------------------------------------------

    def _update_status(self) -> dict[str, Any]:
        """Update duty — supervises the version-gated hot-update boundary."""

        hot_update = self._hot_update or getattr(self.app, "hot_update_service", None)
        if hot_update is None:
            return {"duty": "update", "enabled": False}
        get_status = getattr(hot_update, "status", None)
        if callable(get_status):
            try:
                return {"duty": "update", **get_status()}
            except Exception:
                return {"duty": "update", "available": True}
        return {"duty": "update", "available": True}

    def _automatic_repair_status(self) -> dict[str, Any]:
        """Automatic-repair duty — coordinates the governed repair service."""

        repair = self._repair_service
        if repair is None:
            return {
                "duty": "automatic-repair",
                "enabled": False,
                "delegation": "governed-executor-only",
            }
        get_status = getattr(repair, "status", None)
        if callable(get_status):
            try:
                return {"duty": "automatic-repair", **get_status()}
            except Exception:
                return {"duty": "automatic-repair", "enabled": True}
        return {"duty": "automatic-repair", "enabled": True}

    def _fault_determination_status(self) -> dict[str, Any]:
        """Fault-determination duty — surfaces the repair planner readiness."""

        repair = self._repair_service
        return {
            "duty": "fault-determination",
            "enabled": repair is not None,
            "decision": decision_basis("maintenance")["edicts"],
        }

    def _backup_status(self) -> dict[str, Any]:
        """Backup duty — coordinates governed backup/backup-extraction executor."""

        repair = self._repair_service
        backup_enabled = repair is not None and hasattr(repair, "plan_repair")
        return {
            "duty": "backup",
            "enabled": bool(backup_enabled),
            "delegation": "governed-executor-only",
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _daily_cleaner_status(self) -> dict[str, Any]:
        if self._daily_cleaner is None:
            return {"enabled": False}
        get_status = getattr(self._daily_cleaner, "status", None)
        if callable(get_status):
            return get_status()
        return {"enabled": True}

    async def _resource_loop(self) -> None:
        while not self._stop_requested():
            try:
                await asyncio.sleep(self._resource_interval_seconds)
            except asyncio.CancelledError:
                raise
            release = self.resource_release
            if release is None:
                continue
            try:
                await asyncio.to_thread(release)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

    def _stop_requested(self) -> bool:
        return not self._started

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()


def _suppress(*exceptions: type[BaseException]) -> Any:
    import contextlib

    return contextlib.suppress(*exceptions)


__all__ = ["MAINTENANCE_RESPONSIBILITIES", "MaintenanceSovereign"]
