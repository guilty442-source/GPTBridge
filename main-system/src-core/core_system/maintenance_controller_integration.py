"""Maintenance Controller Integration.

Integrates Database Auto Maintenance v1 with GPTBridgeApp lifecycle:
- Startup: after governance validated, security validated, database foundation ready
- Shutdown: graceful stop with bounded wait
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from typing import Any

from shared_layer.database.config import DatabaseSettings
from shared_layer.database.maintenance import (
    MaintenanceController,
    ControllerConfig,
    SchedulerConfig,
)
from shared_layer.database.maintenance.maintenance_postgres import (
    get_pg_maintenance_executors,
    build_pg_signals as build_pg_maintenance_signals,
    collect_pg_health,
    collect_pool_pressure as collect_pg_pool_pressure,
    collect_lock_pressure as collect_pg_lock_pressure,
    collect_statistics_freshness as collect_pg_statistics_freshness,
)
from shared_layer.database.maintenance.maintenance_sqlite import (
    get_sqlite_maintenance_executors,
    build_sqlite_signals as build_sqlite_maintenance_signals,
    collect_sqlite_health,
)
from shared_layer.database.maintenance.maintenance_reconcile import (
    get_reconcile_maintenance_executors,
    build_reconcile_signals as build_reconcile_maintenance_signals,
    collect_reconcile_state,
)
from shared_layer.database.maintenance.maintenance_backup import (
    get_backup_maintenance_executors,
    build_backup_signals as build_backup_maintenance_signals,
    collect_backup_info,
)
from shared_layer.database.sqlite_classification import list_by_class
from shared_layer.database.workload_lanes import WorkloadClass, get_lane_pool


@dataclass
class MaintenanceControllerIntegration:
    """Maintenance controller integration with GPTBridgeApp."""

    app: Any
    controller: MaintenanceController | None = None
    _started: bool = False
    _native_shadow: Any = None
    # §1.1: True when the automation core drives run_once on the shared
    # scheduler; False when the flow was denied (kill switch); None when
    # the controller still uses its private thread (no core present).
    _core_driven: bool | None = None
    _start_time: float = 0.0
    _persist_failures: int = 0
    # §10.63 R2: both controller callbacks probed PG health independently —
    # ~2 fresh admin connects per call, twice per 30 s tick.  A TTL under
    # the tick interval shares one probe per tick; a failed probe is cached
    # too, so fail-closed semantics surface unchanged.
    _pg_health_cache: tuple[float, Any] | None = None

    async def start(self) -> dict[str, Any]:
        """Start the maintenance controller."""
        if self._started:
            return {"ok": True, "already_started": True}

        self._start_time = time.monotonic()

        # Verify prerequisites
        prereq_check = self._check_prerequisites()
        if not prereq_check["ok"]:
            return {"ok": False, "reason": prereq_check["reason"]}

        # Create controller config
        config = self._create_controller_config()

        # Create controller
        self.controller = MaintenanceController(config)

        # §10.65 act-1: attach the native-shadow observer. Fail-closed —
        # from_policy returns None when the flag is not "shadow" or the
        # native extension is absent, and the Python path never depends on it.
        try:
            from pathlib import Path

            from tasks.maintenance_controller_native_shadow import (
                MaintenanceNativeShadow,
            )

            get_gen = config.get_current_generation
            self._native_shadow = MaintenanceNativeShadow.from_policy(
                Path(getattr(self.app, "project_root", "E:/GPTBridge")),
                scheduler_config=config.scheduler_config,
                current_generation=int(get_gen()) if get_gen else 0,
            )
            self.controller.set_native_shadow(self._native_shadow)
        except Exception:
            self._native_shadow = None

        # Register executors
        self._register_executors()

        # Set callbacks
        self._set_callbacks()

        # Start controller.
        # §1.1 自動化集中：when the automation core is present it owns the
        # cadence — the controller runs without its private thread and each
        # scheduler tick drives ``run_once`` on a worker thread. A denied
        # registration (unlisted/kill-switched) must not fall back to the
        # private loop; the controller then reports started-but-not-driven.
        core = getattr(self.app, "automation_core", None)
        if core is not None:
            self.controller.start(spawn_loop=False)

            async def _driven_tick() -> None:
                controller = self.controller
                if controller is None or not controller.is_running():
                    return
                await asyncio.to_thread(controller.run_once)

            self._core_driven = core.register_flow(
                "maintenance-controller", _driven_tick
            )
        else:
            self.controller.start()
            self._core_driven = None

        self._started = True

        return {
            "ok": True,
            "started_at": time.time(),
            "duration_ms": int((time.monotonic() - self._start_time) * 1000),
            "loop": (
                "automation-core" if self._core_driven
                else ("disabled" if core is not None else "private-thread")
            ),
        }

    async def stop(self) -> dict[str, Any]:
        """Stop the maintenance controller gracefully."""
        if not self._started or not self.controller:
            return {"ok": True, "already_stopped": True}

        stop_start = time.monotonic()

        # §1.1: release the core registration before stopping so no
        # in-flight tick can re-enter a stopped controller.
        core = getattr(self.app, "automation_core", None)
        if core is not None:
            core.unregister("maintenance-controller")

        # Graceful stop
        self.controller.stop(graceful=True)

        self._started = False
        self.controller = None

        return {
            "ok": True,
            "stopped_at": time.time(),
            "duration_ms": int((time.monotonic() - stop_start) * 1000),
        }

    def _check_prerequisites(self) -> dict[str, Any]:
        """Check if prerequisites are met for maintenance controller."""
        # Governance validated
        if not getattr(self.app, "governance", None):
            return {"ok": False, "reason": "Governance not initialized"}

        # Database foundation ready
        try:
            settings = DatabaseSettings.from_environment()
            health_check = getattr(self.app, "_health_check_result", None)
            if health_check and not health_check.get("postgresql", {}).get("ready", False):
                return {"ok": False, "reason": "PostgreSQL not ready"}
        except Exception as exc:
            return {"ok": False, "reason": f"Database settings error: {exc}"}

        # Authority readiness known
        if not getattr(self.app, "decision_sovereign", None):
            return {"ok": False, "reason": "Decision sovereign not started"}

        return {"ok": True}

    def _create_controller_config(self) -> ControllerConfig:
        """Create controller configuration."""
        scheduler_config = SchedulerConfig(
            tick_interval_seconds=30.0,
            max_queued_jobs=100,
            max_job_age_seconds=3600.0,
            admit_m0_always=True,
            admit_m1_when_idle=True,
            admit_m2_with_auth=True,
            m3_candidate_only=True,
            max_retry_attempts=3,
            retry_backoff_base_seconds=60.0,
            enforce_generation_match=True,
        )

        return ControllerConfig(
            scheduler_config=scheduler_config,
            max_concurrent_executions=2,
            execution_timeout_buffer_seconds=30.0,
            shutdown_timeout_seconds=60.0,
            max_shutdown_wait_seconds=300.0,
            pause_on_recovery=True,
            revalidate_on_generation_change=True,
        )

    def _register_executors(self) -> None:
        """Register engine executors with the controller."""
        if not self.controller:
            return

        # PostgreSQL executors
        pg_executors = get_pg_maintenance_executors()
        for action_id, executor in pg_executors.items():
            self.controller.set_executor(action_id, executor)

        # SQLite executors
        sqlite_executors = get_sqlite_maintenance_executors()
        for action_id, executor in sqlite_executors.items():
            self.controller.set_executor(action_id, executor)

        # Reconcile executors
        reconcile_executors = get_reconcile_maintenance_executors()
        for action_id, executor in reconcile_executors.items():
            self.controller.set_executor(action_id, executor)

        # Backup executors
        backup_executors = get_backup_maintenance_executors()
        for action_id, executor in backup_executors.items():
            self.controller.set_executor(action_id, executor)

    def _set_callbacks(self) -> None:
        """Set persistence and state callbacks."""
        if not self.controller:
            return

        self.controller.set_callbacks(
            persist_job=self._persist_job,
            load_pending_jobs=self._load_pending_jobs,
            get_current_generation=self._get_current_generation,
            get_system_state=self._get_system_state,
            get_telemetry_signals=self._get_telemetry_signals,
        )

    def _persist_job(self, job: Any) -> None:
        """Persist maintenance job to PostgreSQL."""
        try:
            from psycopg.types.json import Jsonb

            settings = DatabaseSettings.from_environment()
            with get_lane_pool().connection(WorkloadClass.BACKGROUND) as conn:
                conn.execute(
                    """
                    INSERT INTO gptbridge_maintenance.maintenance_jobs
                    (job_id, action_id, action_version, engine, database_id, module_id,
                     risk_class, priority, status, generation, attempt_count,
                     scheduled_at, started_at, completed_at, lease_until,
                     before_state, after_state, result_code, error_code)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (job_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        attempt_count = EXCLUDED.attempt_count,
                        started_at = EXCLUDED.started_at,
                        completed_at = EXCLUDED.completed_at,
                        lease_until = EXCLUDED.lease_until,
                        before_state = EXCLUDED.before_state,
                        after_state = EXCLUDED.after_state,
                        result_code = EXCLUDED.result_code,
                        error_code = EXCLUDED.error_code,
                        updated_at = now()
                    """,
                    (
                        str(job.job_id),
                        job.action_id,
                        job.action_version,
                        job.engine,
                        job.database_id,
                        job.module_id,
                        job.risk_class.value,
                        job.priority,
                        job.status.value,
                        job.generation,
                        job.attempt_count,
                        job.scheduled_at,
                        job.started_at,
                        job.completed_at,
                        job.lease_until,
                        Jsonb(job.before_state or {}),
                        Jsonb(job.after_state or {}),
                        job.result_code,
                        job.error_code,
                    ),
                )
                # The pool rolls back open transactions on release; a write
                # must commit explicitly or the job record is lost.
                conn.commit()
        except Exception:
            # Log but don't fail - maintenance should not block normal operations.
            # Failures stay countable so a broken schema/grant is observable.
            self._persist_failures += 1

    def _load_pending_jobs(self) -> list[Any]:
        """Load pending maintenance jobs from PostgreSQL."""
        try:
            settings = DatabaseSettings.from_environment()
            # §10.5 殘項：單次唯讀走 pool.execute 一次性介面。
            rows = get_lane_pool().execute(
                WorkloadClass.BACKGROUND,
                """
                SELECT job_id, action_id, action_version, engine, database_id, module_id,
                       risk_class, priority, status, generation, attempt_count,
                       scheduled_at, started_at, completed_at, lease_until,
                       before_state, after_state, result_code, error_code
                FROM gptbridge_maintenance.maintenance_jobs
                WHERE status IN ('PLANNED', 'QUEUED', 'RUNNING', 'VERIFYING', 'DEFERRED')
                ORDER BY priority, scheduled_at
                """,
            )
            from shared_layer.database.maintenance.models import (
                MaintenanceJob,
                MaintenanceJobStatus,
                MaintenanceRiskClass,
            )
            from uuid import UUID

            jobs = []
            for row in rows:
                jobs.append(MaintenanceJob(
                    job_id=UUID(str(row[0])),
                    action_id=row[1],
                    action_version=row[2],
                    engine=row[3],
                    database_id=row[4],
                    module_id=row[5],
                    risk_class=MaintenanceRiskClass(row[6]),
                    priority=row[7],
                    status=MaintenanceJobStatus(row[8]),
                    generation=row[9],
                    attempt_count=row[10],
                    scheduled_at=row[11],
                    started_at=row[12],
                    completed_at=row[13],
                    lease_until=row[14],
                    before_state=row[15] or {},
                    after_state=row[16] or {},
                    result_code=row[17] or "",
                    error_code=row[18] or "",
                ))
            return jobs
        except Exception:
            return []

    def _get_current_generation(self) -> int:
        """Get current system generation."""
        try:
            from shared_layer.database.recovery_orchestrator import get_current_generation
            with get_lane_pool().connection(WorkloadClass.BACKGROUND) as conn:
                return get_current_generation(conn)
        except Exception:
            return 0

    _PG_HEALTH_TTL_S = 20.0

    def _pg_health(self, settings: DatabaseSettings) -> Any:
        now = time.monotonic()
        cached = self._pg_health_cache
        hit = cached is not None and now - cached[0] < self._PG_HEALTH_TTL_S
        shadow = getattr(self, "_native_shadow", None)
        if shadow is not None:
            try:
                shadow.observe_probe_cache(
                    now_s=now, ttl_s=self._PG_HEALTH_TTL_S, py_hit=hit
                )
            except Exception:
                pass
        if hit:
            return cached[1]
        health = collect_pg_health(settings)
        self._pg_health_cache = (now, health)
        if shadow is not None:
            try:
                shadow.observe_probe_store(
                    now_s=now,
                    probe_ok=bool(getattr(health, "available", False)),
                )
            except Exception:
                pass
        return health

    def _get_system_state(self) -> dict[str, Any]:
        """Get current system state for policy evaluation."""
        generation = self._get_current_generation()
        state = {
            "recovery_state": "NORMAL",
            "pg_healthy": True,
            "pg_latency_ms": 0,
            "pg_lock_pressure": 0,
            "transport_backlog": 0,
            "transport_oldest_pending_age_seconds": 0,
            "disk_pressure": 0,
            "current_generation": generation,
            "job_generation": generation,
            "maintenance_cooldown_active": False,
            "active_lease_conflict": False,
            "shutdown_draining": getattr(self.app, "_shutdown_started", False),
            "governed_authorization": False,  # Would be set by decision sovereign
        }

        # Check recovery state
        try:
            from shared_layer.database.recovery_orchestrator import is_recovery_barrier_active
            with get_lane_pool().connection(WorkloadClass.BACKGROUND) as conn:
                if is_recovery_barrier_active(conn):
                    state["recovery_state"] = "RECOVERING"
        except Exception:
            pass

        # Get PostgreSQL health
        try:
            settings = DatabaseSettings.from_environment()
            health = self._pg_health(settings)
            state["pg_healthy"] = health.available
            state["pg_latency_ms"] = health.latency_ms
            state["pg_lock_pressure"] = health.lock_pressure
        except Exception:
            pass

        return state

    def _get_telemetry_signals(self) -> dict[str, Any]:
        """Collect telemetry signals for all evaluators."""
        signals = {}

        # PostgreSQL signals
        try:
            settings = DatabaseSettings.from_environment()
            health = self._pg_health(settings)
            from shared_layer.database.connection import peek_connection_manager

            pool = collect_pg_pool_pressure(peek_connection_manager())
            locks = collect_pg_lock_pressure(settings)
            stats = collect_pg_statistics_freshness(settings)
            signals.update(build_pg_maintenance_signals(health, pool, locks, stats))
        except Exception:
            pass

        # SQLite signals
        try:
            import sqlite3
            from pathlib import Path

            sqlite_dbs: list[dict[str, Any]] = []
            with get_lane_pool().connection(WorkloadClass.BACKGROUND) as registry_conn:
                for db_class in ["A", "B", "C", "D"]:
                    sqlite_dbs.extend(
                        list_by_class(registry_conn, db_class=db_class)
                    )
            if sqlite_dbs:
                health_metrics = []
                for db in sqlite_dbs:
                    database_path = str(db.get("database_path") or "")
                    if not database_path or not Path(database_path).is_file():
                        continue
                    try:
                        connection = sqlite3.connect(
                            f"file:{Path(database_path).as_posix()}?mode=ro",
                            uri=True,
                        )
                        try:
                            health = collect_sqlite_health(
                                connection,
                                str(db.get("module_id") or ""),
                                database_path,
                            )
                        finally:
                            connection.close()
                        health_metrics.append(health)
                    except Exception:
                        continue
                if health_metrics:
                    signals.update(build_sqlite_maintenance_signals(health_metrics))
        except Exception:
            pass

        # Reconcile signals
        try:
            reconcile_state = collect_reconcile_state()
            signals.update(build_reconcile_maintenance_signals(reconcile_state))
        except Exception:
            pass

        # Backup signals
        try:
            from pathlib import Path

            default_backup_dir = (
                Path(getattr(self.app, "project_root", "E:/GPTBridge"))
                / ".backups"
                / "git"
            )
            backup_dir = Path(
                os.environ.get("GPTBRIDGE_BACKUP_DIR") or default_backup_dir
            )
            signals.update(build_backup_maintenance_signals(backup_dir))
        except Exception:
            pass

        # Adaptive control-plane observation (bounded, ALLOW until signals
        # persist; the plane never changes behaviour without observed load).
        try:
            from shared_layer.adaptive import LoadSignals, get_plane

            get_plane().observe(
                LoadSignals(
                    pg_latency_ms=float(signals.get("pg_latency_ms") or 0.0),
                    lock_contention_pct=float(signals.get("lock_pressure") or 0.0),
                    active_connections=int(signals.get("pg_connections") or 0),
                    transport_backlog=int(signals.get("transport_backlog") or 0),
                    reconcile_backlog=int(signals.get("reconcile_pending") or 0),
                )
            )
            # S7: push the tuned pool bound into the live connection manager
            # (peek — never construct the pool just to tune it).
            from shared_layer.database.connection import peek_connection_manager

            pool = peek_connection_manager()
            if pool is not None:
                get_plane().tuner.apply_pool_limits(pool)
        except Exception:
            pass

        return signals

    def get_status(self) -> dict[str, Any]:
        """Get maintenance controller status."""
        if not self.controller:
            return {"running": False, "persist_failures": self._persist_failures}
        return {
            **self.controller.get_status(),
            "persist_failures": self._persist_failures,
        }


def create_maintenance_controller_integration(app: Any) -> MaintenanceControllerIntegration:
    """Create maintenance controller integration for the app."""
    return MaintenanceControllerIntegration(app)


__all__ = [
    "MaintenanceControllerIntegration",
    "create_maintenance_controller_integration",
]