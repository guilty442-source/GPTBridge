"""SQLite Maintenance v1.

Class-aware maintenance for SQLite databases:

Class A: Governance codex / high-integrity
  - Only: existence check, read-only check, schema check, integrity verification, hash verification, backup verification
  - NO content modifications allowed

Class B: Module-private formal state
  - Health, WAL observation, safe checkpoint, ANALYZE candidate, backup verification

Class C: Runtime / checkpoint
  - Checkpoint, stale runtime cleanup, bounded rotation

Class D: Cache / fallback
  - Checkpoint, stale cache cleanup, rebuildable cache reset

No automatic VACUUM for all SQLite databases.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from .models import MaintenanceJob
from .registry import MaintenanceAction
from .verifier import verify_sqlite_checkpoint, verify_health_observe
from ..sqlite_classification import get_class
from ..sqlite_wal_governor import get_wal_stats, check_and_checkpoint
from ..workload_lanes import WorkloadClass, get_lane_pool


class SQLiteClass(Enum):
    """SQLite database classification."""

    A = "A"  # Governance codex / high-integrity
    B = "B"  # Module-private formal state
    C = "C"  # Runtime / checkpoint
    D = "D"  # Cache / fallback


@dataclass(frozen=True)
class SQLiteClassPolicy:
    """Policy per SQLite class."""

    class_id: SQLiteClass
    allow_checkpoint: bool
    allow_analyze: bool
    allow_cleanup: bool
    allow_rotation: bool
    allow_rebuild: bool
    checkpoint_threshold_mb: float
    integrity_check_required: bool
    backup_verify_required: bool


CLASS_POLICIES = {
    SQLiteClass.A: SQLiteClassPolicy(
        class_id=SQLiteClass.A,
        allow_checkpoint=False,
        allow_analyze=False,
        allow_cleanup=False,
        allow_rotation=False,
        allow_rebuild=False,
        checkpoint_threshold_mb=0,
        integrity_check_required=True,
        backup_verify_required=True,
    ),
    SQLiteClass.B: SQLiteClassPolicy(
        class_id=SQLiteClass.B,
        allow_checkpoint=True,
        allow_analyze=True,
        allow_cleanup=False,
        allow_rotation=False,
        allow_rebuild=False,
        checkpoint_threshold_mb=50,
        integrity_check_required=True,
        backup_verify_required=True,
    ),
    SQLiteClass.C: SQLiteClassPolicy(
        class_id=SQLiteClass.C,
        allow_checkpoint=True,
        allow_analyze=False,
        allow_cleanup=True,
        allow_rotation=True,
        allow_rebuild=False,
        checkpoint_threshold_mb=20,
        integrity_check_required=False,
        backup_verify_required=False,
    ),
    SQLiteClass.D: SQLiteClassPolicy(
        class_id=SQLiteClass.D,
        allow_checkpoint=True,
        allow_analyze=False,
        allow_cleanup=True,
        allow_rotation=True,
        allow_rebuild=True,
        checkpoint_threshold_mb=10,
        integrity_check_required=False,
        backup_verify_required=False,
    ),
}


@dataclass(frozen=True)
class SQLiteHealthMetrics:
    """SQLite health metrics."""

    database_path: str
    module_id: str
    db_class: SQLiteClass
    accessible: bool
    wal_size_mb: float
    db_size_mb: float
    page_count: int
    journal_mode: str
    integrity_ok: bool
    readonly: bool
    error: str | None = None


def get_sqlite_class_policy(db_class: str) -> SQLiteClassPolicy:
    """Get policy for a SQLite class."""
    try:
        return CLASS_POLICIES[SQLiteClass(db_class)]
    except (ValueError, KeyError):
        return CLASS_POLICIES[SQLiteClass.D]


def collect_sqlite_health(
    connection: sqlite3.Connection,
    module_id: str,
    database_path: str,
) -> SQLiteHealthMetrics:
    """Collect SQLite health metrics."""
    try:
        # Get classification from PostgreSQL registry
        class_info = None
        try:
            with get_lane_pool().connection(WorkloadClass.BACKGROUND) as pg_conn:
                class_info = get_class(pg_conn, module_id=module_id, database_path=database_path)
        except Exception:
            pass

        db_class_str = class_info.get("db_class", "D") if class_info else "D"
        db_class = SQLiteClass(db_class_str)

        stats = get_wal_stats(connection)

        # Integrity check (quick)
        integrity_ok = True
        try:
            result = connection.execute("PRAGMA quick_check").fetchone()
            integrity_ok = result and result[0] == "ok"
        except Exception:
            integrity_ok = False

        # Read-only check
        readonly = False
        try:
            result = connection.execute("PRAGMA query_only").fetchone()
            readonly = bool(result[0]) if result else False
        except Exception:
            pass

        return SQLiteHealthMetrics(
            database_path=database_path,
            module_id=module_id,
            db_class=db_class,
            accessible=True,
            wal_size_mb=stats.get("wal_size_bytes", 0) / (1024 * 1024),
            db_size_mb=stats.get("db_size_bytes", 0) / (1024 * 1024),
            page_count=stats.get("page_count", 0),
            journal_mode=stats.get("journal_mode", "unknown"),
            integrity_ok=integrity_ok,
            readonly=readonly,
        )

    except Exception as exc:
        return SQLiteHealthMetrics(
            database_path=database_path,
            module_id=module_id,
            db_class=SQLiteClass.D,
            accessible=False,
            wal_size_mb=0,
            db_size_mb=0,
            page_count=0,
            journal_mode="unknown",
            integrity_ok=False,
            readonly=True,
            error=str(exc)[:200],
        )


def check_sqlite_checkpoint_allowed(
    health: SQLiteHealthMetrics,
    policy: SQLiteClassPolicy,
) -> tuple[bool, str]:
    """Check if checkpoint is allowed for this database."""
    if not policy.allow_checkpoint:
        return False, f"Checkpoint not allowed for Class {health.db_class.value}"

    if not health.accessible:
        return False, "Database not accessible"

    if health.readonly:
        return False, "Database is read-only"

    if not health.integrity_ok:
        return False, "Integrity check failed"

    return True, "OK"


def execute_sqlite_checkpoint(
    job: MaintenanceJob,
    action: MaintenanceAction,
    before_state: dict[str, Any],
) -> dict[str, Any]:
    """Execute SQLite checkpoint based on WAL size."""
    after_state = dict(before_state)

    database_path = job.database_id
    module_id = job.module_id

    try:
        # Open connection
        conn = sqlite3.connect(database_path)

        # Get class policy
        health_before = collect_sqlite_health(conn, module_id, database_path)
        policy = get_sqlite_class_policy(health_before.db_class.value)

        allowed, reason = check_sqlite_checkpoint_allowed(health_before, policy)
        if not allowed:
            after_state["checkpoint_completed"] = False
            after_state["error"] = reason
            return after_state

        # Run checkpoint via WAL governor
        stats = check_and_checkpoint(
            conn,
            wal_size_threshold_mb=policy.checkpoint_threshold_mb,
            force_truncate_at_mb=policy.checkpoint_threshold_mb * 4,
        )

        # Collect after state
        health_after = collect_sqlite_health(conn, module_id, database_path)

        after_state["checkpoint_completed"] = True
        after_state["action_taken"] = stats.get("action", "passive")
        after_state["wal_size_mb"] = health_after.wal_size_mb
        after_state["db_size_mb"] = health_after.db_size_mb
        after_state["integrity_ok"] = health_after.integrity_ok
        after_state["db_accessible"] = health_after.accessible
        after_state["integrity_worsened"] = not health_after.integrity_ok and health_before.integrity_ok
        after_state["maintenance_verified"] = True

    except Exception as exc:
        after_state["checkpoint_completed"] = False
        after_state["error"] = str(exc)[:300]
        after_state["db_accessible"] = False

    return after_state


def execute_sqlite_health_observe(
    job: MaintenanceJob,
    action: MaintenanceAction,
    before_state: dict[str, Any],
) -> dict[str, Any]:
    """Execute SQLite health observation."""
    after_state = dict(before_state)

    database_path = job.database_id
    module_id = job.module_id

    try:
        conn = sqlite3.connect(database_path)
        health = collect_sqlite_health(conn, module_id, database_path)

        after_state["metrics_collected"] = True
        after_state["health_metrics"] = {
            "db_class": health.db_class.value,
            "accessible": health.accessible,
            "wal_size_mb": health.wal_size_mb,
            "db_size_mb": health.db_size_mb,
            "journal_mode": health.journal_mode,
            "integrity_ok": health.integrity_ok,
            "readonly": health.readonly,
        }
        after_state["maintenance_verified"] = True

    except Exception as exc:
        after_state["metrics_collected"] = False
        after_state["error"] = str(exc)[:200]

    return after_state


def execute_sqlite_cleanup(
    job: MaintenanceJob,
    action: MaintenanceAction,
    before_state: dict[str, Any],
) -> dict[str, Any]:
    """Execute SQLite stale data cleanup (Class C/D only)."""
    after_state = dict(before_state)

    database_path = job.database_id
    module_id = job.module_id

    try:
        with get_lane_pool().connection(WorkloadClass.BACKGROUND) as pg_conn:
            class_info = get_class(pg_conn, module_id=module_id, database_path=database_path)
            if not class_info:
                after_state["cleanup_completed"] = False
                after_state["error"] = "Database not registered"
                return after_state

            db_class = SQLiteClass(class_info.get("db_class", "D"))
            policy = get_sqlite_class_policy(db_class)

            if not policy.allow_cleanup:
                after_state["cleanup_completed"] = False
                after_state["error"] = f"Cleanup not allowed for Class {db_class.value}"
                return after_state

        # Actual cleanup would be module-specific
        # For now, just mark as candidate
        after_state["cleanup_completed"] = False
        after_state["error"] = "Cleanup requires module-specific implementation"

    except Exception as exc:
        after_state["cleanup_completed"] = False
        after_state["error"] = str(exc)[:300]

    return after_state


def execute_sqlite_rotation(
    job: MaintenanceJob,
    action: MaintenanceAction,
    before_state: dict[str, Any],
) -> dict[str, Any]:
    """Execute SQLite bounded rotation (Class C/D only)."""
    after_state = dict(before_state)
    after_state["rotation_completed"] = False
    after_state["error"] = "Rotation requires module-specific implementation"
    return after_state


def execute_sqlite_rebuild(
    job: MaintenanceJob,
    action: MaintenanceAction,
    before_state: dict[str, Any],
) -> dict[str, Any]:
    """Execute SQLite cache rebuild (Class D only)."""
    after_state = dict(before_state)
    after_state["rebuild_completed"] = False
    after_state["error"] = "Rebuild requires module-specific implementation"
    return after_state


def build_sqlite_signals(
    health_metrics: list[SQLiteHealthMetrics],
) -> dict[str, Any]:
    """Build telemetry signals for SQLite evaluator."""
    signals = {}
    for health in health_metrics:
        key = f"sqlite_{health.module_id}_{health.database_path.replace('/', '_').replace('.', '_')}"
        signals[f"{key}_wal_mb"] = health.wal_size_mb
        signals[f"{key}_db_mb"] = health.db_size_mb
        signals[f"{key}_db_class"] = health.db_class.value
        signals[f"{key}_accessible"] = health.accessible
        signals[f"{key}_integrity_ok"] = health.integrity_ok

        # Check for blocked writers (would need more instrumentation)
        signals[f"{key}_blocked_writer"] = False
        signals[f"{key}_long_reader"] = False

    signals["sqlite_checkpoint_threshold_mb"] = 50
    signals["disk_healthy"] = True  # Would come from disk monitor

    return signals


def get_sqlite_maintenance_executors() -> dict[str, callable]:
    """Get SQLite maintenance executors for the controller."""
    return {
        "sqlite_checkpoint_v1": execute_sqlite_checkpoint,
        "sqlite_health_observe_v1": execute_sqlite_health_observe,
        "sqlite_cleanup_v1": execute_sqlite_cleanup,
        "sqlite_rotation_v1": execute_sqlite_rotation,
        "sqlite_rebuild_v1": execute_sqlite_rebuild,
    }


__all__ = [
    "SQLiteClass",
    "SQLiteClassPolicy",
    "CLASS_POLICIES",
    "SQLiteHealthMetrics",
    "get_sqlite_class_policy",
    "collect_sqlite_health",
    "check_sqlite_checkpoint_allowed",
    "execute_sqlite_checkpoint",
    "execute_sqlite_health_observe",
    "execute_sqlite_cleanup",
    "execute_sqlite_rotation",
    "execute_sqlite_rebuild",
    "build_sqlite_signals",
    "get_sqlite_maintenance_executors",
]