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
import shutil
import socket
import sys
from pathlib import Path
from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX

from .codex_decision import decision_basis
from .sovereign_utils import _iso_now, _suppress
from .native import (
    monotonic_seconds,
    native_available,
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

    ROLE = _MAINTENANCE_SOVEREIGN.id

    def __init__(self, app: Any) -> None:
        self.app = app
        self._started = False
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._daily_cleaner: Any | None = None
        self._hot_update: Any | None = None
        self._repair_service: Any | None = None
        self._health_checker: Any = check_core_health
        self._capability_task: asyncio.Task[Any] | None = None
        self._capability_interval_seconds = 3600.0
        self._capability_report: dict[str, Any] | None = None
        # Persistent learning state — survives auto-repair restarts because
        # it is backed by the SQLite repair-learning store.  The sovereign
        # loads the last analysis on start so its knowledge is not reset by
        # a backend crash/restart cycle.
        self._learning_store: Any | None = None
        self._learner: Any | None = None
        self._learning_analysis: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Persistent learning (survives auto-repair restarts)
    # ------------------------------------------------------------------

    def _ensure_learning_store(self) -> None:
        """Lazily attach to the persistent repair-learning SQLite store.

        The store lives under ``main-system/data/automatic-repair`` and is
        shared with ``CentralRepairService``.  By reading from the same
        SQLite database the sovereign's learning state survives backend
        restarts triggered by auto-repair — the in-memory sovereign is
        recreated, but the persisted error signatures, outcomes and learned
        recipes are reloaded on the next ``start()``.
        """
        if self._learning_store is not None:
            return
        try:
            project_root = Path(getattr(self.app, "project_root", ".") or ".")
            repair_data = project_root / "main-system" / "data" / "automatic-repair"
            from tasks.repair_learning import RepairLearner, RepairLearningStore

            self._learning_store = RepairLearningStore(repair_data)
            self._learner = RepairLearner(self._learning_store)
        except Exception:
            # Best-effort: learning is optional and never blocks maintenance.
            self._learning_store = None
            self._learner = None

    def learning_status(self) -> dict[str, Any]:
        """Surface the persistent learning state (not reset by auto-repair)."""
        self._ensure_learning_store()
        if self._learner is None:
            return {
                "enabled": False,
                "reason": "learning-store-unavailable",
                "authority": self.ROLE,
            }
        try:
            if self._learning_analysis is None:
                self._learning_analysis = self._learner.analyze_history()
            analysis = dict(self._learning_analysis)
            analysis["enabled"] = True
            analysis["authority"] = self.ROLE
            analysis["persistence"] = "sqlite-survives-restart"
            return analysis
        except Exception as error:
            return {
                "enabled": True,
                "authority": self.ROLE,
                "error": f"{type(error).__name__}: {error}",
                "persistence": "sqlite-survives-restart",
            }

    def record_repair_outcome(
        self,
        *,
        error_class: str,
        message: str,
        failure_code: str,
        remedy: str,
        ok: bool,
        file_path: str = "",
        target_tool_id: str = "main-system",
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a repair outcome in the persistent learning store.

        The sovereign coordinates learning: when a governed executor (boot_core,
        CentralRepairService, ConnectionWatchdog) completes a repair, the
        sovereign records the outcome so recurring error→remedy patterns can
        be promoted to learned recipes.  This state is persisted in SQLite and
        is not reset by subsequent auto-repair restarts.
        """
        self._ensure_learning_store()
        if self._learner is None:
            return {"recorded": False, "reason": "learning-store-unavailable"}
        try:
            from tasks.repair_learning import ErrorSignature, RepairOutcome
            from uuid import uuid4

            from tasks.repair_learning import _normalize_error_signature

            signature_hash = _normalize_error_signature(
                error_class, message, file_path=file_path
            )
            signature = ErrorSignature(
                signature_hash=signature_hash,
                error_class=error_class,
                message_pattern=message[:200],
                failure_code=failure_code,
                file_context=file_path,
                target_tool_id=target_tool_id,
            )
            outcome = RepairOutcome(
                run_id=run_id or uuid4().hex,
                signature_hash=signature_hash,
                remedy=remedy,
                ok=ok,
                detail=detail or {},
            )
            promotion = self._learner.learn_from_outcome(signature, outcome)
            # Invalidate cached analysis so the next status call refreshes.
            self._learning_analysis = None
            return {
                "recorded": True,
                "signature_hash": signature_hash,
                "promotion": promotion,
                "persistence": "sqlite-survives-restart",
            }
        except Exception as error:
            return {
                "recorded": False,
                "error": f"{type(error).__name__}: {error}",
            }

    def suggest_remedy(
        self,
        *,
        error_class: str,
        message: str,
        failure_code: str = "",
        file_path: str = "",
    ) -> dict[str, Any]:
        """Query the learning store for the best known remedy for an error."""
        self._ensure_learning_store()
        if self._learner is None:
            return {"suggested": False, "reason": "learning-store-unavailable"}
        try:
            from tasks.repair_learning import ErrorSignature
            from tasks.repair_learning import _normalize_error_signature

            signature_hash = _normalize_error_signature(
                error_class, message, file_path=file_path
            )
            signature = ErrorSignature(
                signature_hash=signature_hash,
                error_class=error_class,
                message_pattern=message[:200],
                failure_code=failure_code,
                file_context=file_path,
            )
            return self._learner.suggest_remedy(signature)
        except Exception as error:
            return {
                "suggested": False,
                "error": f"{type(error).__name__}: {error}",
            }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(
        self,
        *,
        daily_cleaner: Any = None,
        hot_update: Any = None,
        repair_service: Any = None,
        health_checker: Any = None,
        capability_interval_seconds: float = 3600.0,
    ) -> dict[str, Any]:
        """Start the Maintenance Sovereign and its in-process maintenance loops.

        ``daily_cleaner``: the DailyGlobalCleanerService instance already built
            and started by the app (its loop runs independently).
        ``hot_update``: the governed HotUpdateService; the sovereign supervises
            the frozen, version-gated update boundary (decision only).
        ``repair_service``: the governed CentralRepairService; the sovereign
            surfaces fault-determination, automatic-repair and backup-extraction
            status but never runs the heavy work in-process.
        ``health_checker``: a callable returning a health report dict (default
            ``core.health.check_core_health``), used for system-health monitoring.
        ``capability_interval_seconds``: interval for the independent
            capability/installation check loop (default hourly).  The check is
            read-only detection only; it never installs anything.
        """

        self._daily_cleaner = daily_cleaner
        self._hot_update = hot_update
        self._repair_service = repair_service
        if health_checker is not None:
            self._health_checker = health_checker
        self._capability_interval_seconds = max(
            300.0, float(capability_interval_seconds)
        )
        self._started_at = _iso_now()
        self._started = True

        # Reload persistent learning state so the sovereign's knowledge is
        # not reset by an auto-repair restart.  The SQLite store survives
        # backend crashes; the in-memory sovereign is recreated but rehydrates
        # from the persisted error signatures and learned recipes.
        self._ensure_learning_store()
        if self._learner is not None:
            try:
                self._learning_analysis = self._learner.analyze_history()
            except Exception:
                self._learning_analysis = None

        if self._capability_task is None:
            self._capability_task = asyncio.create_task(
                self._capability_check_loop(),
                name="maintenance-sovereign-capability",
            )

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "responsibilities": list(MAINTENANCE_RESPONSIBILITIES),
            "daily_cleaner": self._daily_cleaner_status(),
        }

    async def stop(self) -> None:
        if self._capability_task is not None:
            self._capability_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._capability_task
            self._capability_task = None
        self._capability_report = None
        self._daily_cleaner = None
        self._hot_update = None
        self._repair_service = None
        self._started = False
        self._stopped_at = _iso_now()

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
            "module_cleanup": self._module_cleanup_status(),
            "main_system_self_maintenance": self._main_system_self_maintenance_status(),
            "learning": self.learning_status(),
            "capability_loop": {
                "running": self._capability_task is not None and not self._capability_task.done(),
                "interval_seconds": self._capability_interval_seconds,
            },
            "native": resource_status(),
            "decision": decision_basis(_MAINTENANCE_SOVEREIGN.area),
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
            "module_cleanup": self._module_cleanup_status(),
            "main_system_self_maintenance": self._main_system_self_maintenance_status(),
            "learning": self.learning_status(),
            "delegation": "governed-executor-only",
            "native_kernel": native_available(),
            "decision": decision_basis(_MAINTENANCE_SOVEREIGN.area),
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
            "decision": decision_basis(_MAINTENANCE_SOVEREIGN.area)["edicts"],
        }

    # ------------------------------------------------------------------
    # Update / automatic-repair / fault-determination / backup surfaces
    # ------------------------------------------------------------------

    def _update_status(self) -> dict[str, Any]:
        """Update duty — supervises the version-gated hot-update boundary and
        the system-wide hot-reload capability."""

        hot_update = self._hot_update or getattr(self.app, "hot_update_service", None)
        if hot_update is None:
            return {"duty": "update-management", "owner": self.ROLE, "enabled": False}
        get_status = getattr(hot_update, "status", None)
        if callable(get_status):
            try:
                return {"duty": "update-management", "owner": self.ROLE, **get_status()}
            except Exception:
                return {"duty": "update-management", "owner": self.ROLE, "available": True}
        return {"duty": "update-management", "owner": self.ROLE, "available": True}

    async def _notify_ui(self, event: str, payload: dict[str, Any]) -> int:
        shells = getattr(self.app, "_active_ui_shells", None) or set()
        if not shells:
            return 0
        count = 0
        for shell in list(shells):
            send = getattr(shell, "send_event", None)
            if not callable(send):
                continue
            try:
                await send(event, payload)
                count += 1
            except Exception:
                pass
        return count

    async def execute_hot_reload(
        self,
        *,
        approval_token: str | None = None,
        modules: Any = None,
    ) -> dict[str, Any]:
        """Coordinate a system-wide hot-reload of governed backend modules.

        Hot-reload is a maintenance operation under the update-management
        duty (A24/E8).  It reloads already-loaded Python modules in-place so
        source edits to governed backend code take effect without a full
        process restart.  Governance authorization is required; the scope is
        system-wide (all backend src roots, not just main-system/src-core).

        After a successful reload the self-maintenance stability check is
        re-run and the frontend is notified so it can refresh in sync.
        """
        hot_update = self._hot_update or getattr(self.app, "hot_update_service", None)
        if hot_update is None:
            return {
                "ok": False,
                "duty": "update-management",
                "error": "hot-update-service-unavailable",
            }
        reload_modules = getattr(hot_update, "reload_modules", None)
        if not callable(reload_modules):
            return {
                "ok": False,
                "duty": "update-management",
                "error": "hot-reload-not-supported",
            }
        governance = getattr(self.app, "governance", None)
        report = await asyncio.to_thread(
            reload_modules,
            governance=governance,
            approval_token=approval_token,
            modules=modules,
        )

        # Sync the frontend so it can refresh against the newly loaded backend.
        notified = await self._notify_ui(
            "maintenance:hot-reload-completed",
            {
                "ok": report.ok,
                "reloaded_count": len(report.reloaded),
                "skipped_count": len(report.skipped),
                "errors": list(report.errors)[:8],
            },
        )

        # Re-run the stability/version maintenance check (read-only) so any
        # source drift introduced by the reload is reported immediately.
        auto_repair: dict[str, Any] = {"ok": True, "skipped": True}
        maintenance = getattr(self.app, "main_system_self_maintenance", None)
        if maintenance is not None and hasattr(maintenance, "run_once"):
            try:
                auto_repair = await maintenance.run_once()
            except Exception as error:
                auto_repair = {
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }

        return {
            "ok": report.ok,
            "duty": "update-management",
            "operation": "hot-reload",
            "authority": self.ROLE,
            "scope": "system-wide",
            "reloaded": list(report.reloaded),
            "skipped": list(report.skipped),
            "errors": list(report.errors),
            "ui_notified": notified,
            "auto_repair": auto_repair,
        }

    def _third_party_update_executor(self) -> Any:
        system_sovereign = getattr(self.app, "system_sovereign_service", None)
        if system_sovereign is None:
            return None
        return getattr(system_sovereign, "third_party_sovereign", None)

    async def execute_third_party_update(
        self, tool_id: str, *, approval_token: str | None = None
    ) -> Any:
        """Manage one update and delegate only its execution."""
        executor = self._third_party_update_executor()
        if executor is None:
            raise RuntimeError("third-party update executor unavailable")
        return await executor.apply_approved_update(
            tool_id, approval_token=approval_token
        )

    async def execute_auto_third_party_updates(
        self, *, approval_token: str, only_available: bool = True
    ) -> dict[str, Any]:
        """Manage approved automatic updates and delegate their execution."""
        executor = self._third_party_update_executor()
        if executor is None:
            raise RuntimeError("third-party update executor unavailable")
        return await executor.apply_approved_auto_updates(
            approval_token=approval_token, only_available=only_available
        )

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
            "decision": decision_basis(_MAINTENANCE_SOVEREIGN.area)["edicts"],
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
    # Capability / installation detection (read-only, independent schedule)
    # ------------------------------------------------------------------

    @staticmethod
    def _probe_tcp(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            return False

    def _run_capability_checks(self) -> dict[str, Any]:
        """Detect whether maintenance-relevant functions/components are present.

        Detection only — per project policy nothing is installed or remediated
        here; missing components are simply reported.
        """

        root = Path(getattr(self.app, "project_root", ".") or ".").resolve()

        functions: dict[str, bool] = {
            "health_checker": callable(self._health_checker),
            "daily_cleaner": self._daily_cleaner is not None,
            "automatic_repair": self._repair_service is not None,
            "hot_update": self._hot_update is not None,
            "resource_release": callable(
                getattr(self, "resource_release", None)
            ),
            "governance": getattr(self.app, "governance", None) is not None,
        }

        components: dict[str, Any] = {
            "python": {"installed": bool(sys.executable)},
            "node": {"installed": shutil.which("node") is not None},
            "npm": {
                "installed": shutil.which("npm") is not None
                or shutil.which("npm.cmd") is not None
            },
            "git": {"installed": shutil.which("git") is not None},
            "ollama": {
                "installed": shutil.which("ollama") is not None,
                "reachable": self._probe_tcp(11434),
            },
            "postgresql": {
                "installed": shutil.which("psql") is not None
                or shutil.which("pg_isready") is not None,
                "reachable": self._probe_tcp(5432),
            },
            "qdrant": {
                "installed": (
                    root / "local-model" / "runtime" / "qdrant"
                ).is_dir(),
                "reachable": self._probe_tcp(6333),
            },
            "shared_layer_data": {
                "installed": (root / "shared-layer" / "data").is_dir()
            },
        }

        missing_functions = sorted(
            name for name, ok in functions.items() if not ok
        )
        missing_components = sorted(
            name
            for name, entry in components.items()
            if isinstance(entry, dict) and not entry.get("installed")
        )
        unreachable = sorted(
            name
            for name, entry in components.items()
            if isinstance(entry, dict)
            and "reachable" in entry
            and not entry.get("reachable")
        )
        ok = not missing_functions and not missing_components
        return {
            "ok": ok,
            "status": "healthy" if ok else "degraded",
            "checked_at": _iso_now(),
            "interval_seconds": self._capability_interval_seconds,
            "mode": "read-only-detection-no-auto-install",
            "functions": functions,
            "missing_functions": missing_functions,
            "components": components,
            "missing_components": missing_components,
            "unreachable_services": unreachable,
        }

    def _capability_status(self) -> dict[str, Any]:
        report = self._capability_report
        if report is not None:
            return dict(report)
        return {
            "ok": None,
            "status": "pending-first-check",
            "mode": "read-only-detection-no-auto-install",
            "interval_seconds": self._capability_interval_seconds,
        }

    async def _capability_check_loop(self) -> None:
        while not self._stop_requested():
            try:
                self._capability_report = await asyncio.to_thread(
                    self._run_capability_checks
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._capability_report = {
                    "ok": False,
                    "status": "check-failed",
                    "checked_at": _iso_now(),
                    "mode": "read-only-detection-no-auto-install",
                    "error": f"{type(error).__name__}: {error}",
                }
            try:
                await asyncio.sleep(self._capability_interval_seconds)
            except asyncio.CancelledError:
                raise


    def _stop_requested(self) -> bool:
        return not self._started

__all__ = ["MAINTENANCE_RESPONSIBILITIES", "MaintenanceSovereign"]
