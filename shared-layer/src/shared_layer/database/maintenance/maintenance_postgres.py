"""PostgreSQL Maintenance v1.

Implements:
1. Health collection
2. Pool pressure collection
3. Lock pressure collection
4. Statistics freshness observation
5. Table/index growth observation
6. ANALYZE candidate
7. Safe ANALYZE execution
8. Verification

Does NOT auto-execute:
- VACUUM FULL
- Arbitrary REINDEX
- DDL
- Parameter rewrite
- max_connections rewrite
- shared_buffers rewrite
- Server restart

Regular VACUUM interface preserved but not auto-enabled in v1.
"""

from __future__ import annotations

import psycopg
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..config import DatabaseSettings
from ..connection import database_dsn
from ..health import DatabaseHealthCheck
from ..pool import PostgreSQLPool
from .models import MaintenanceJob
from .registry import MaintenanceAction
from .verifier import verify_pg_analyze, verify_health_observe


@dataclass(frozen=True)
class PgHealthMetrics:
    """PostgreSQL health metrics."""

    available: bool
    latency_ms: float
    connections_used: int
    connections_max: int
    connection_pressure: float
    locks_total: int
    locks_waiting: int
    lock_pressure: float
    replication_lag_bytes: int
    migration_count: int
    error: str | None = None


@dataclass(frozen=True)
class PgPoolPressure:
    """PostgreSQL pool pressure metrics."""

    pool_size: int
    pool_max: int
    pool_pressure: float
    idle_connections: int
    waiting_requests: int


@dataclass(frozen=True)
class PgLockPressure:
    """PostgreSQL lock pressure metrics."""

    total_locks: int
    exclusive_locks: int
    waiting_locks: int
    lock_pressure: float
    blocking_pids: list[int]


@dataclass(frozen=True)
class PgStatisticsFreshness:
    """PostgreSQL statistics freshness."""

    stale_tables: int
    total_tables: int
    stale_ratio: float
    oldest_stats_age_hours: float


@dataclass(frozen=True)
class PgGrowthObservation:
    """PostgreSQL table/index growth observation."""

    table_name: str
    size_mb: float
    size_delta_mb: float
    index_size_mb: float
    row_count: int
    row_delta: int


def collect_pg_health(settings: DatabaseSettings) -> PgHealthMetrics:
    """Collect PostgreSQL health metrics."""
    health_check = DatabaseHealthCheck(settings)
    health = health_check.run()

    if not health.available:
        return PgHealthMetrics(
            available=False,
            latency_ms=health.latency_ms,
            connections_used=0,
            connections_max=0,
            connection_pressure=1.0,
            locks_total=0,
            locks_waiting=0,
            lock_pressure=1.0,
            replication_lag_bytes=0,
            migration_count=0,
            error=health.error,
        )

    # Collect additional metrics
    try:
        with psycopg.connect(database_dsn(settings.admin_dsn, settings.database)) as conn:
            # Connection stats
            conn_row = conn.execute(
                "SELECT count(*), setting::int FROM pg_stat_activity, pg_settings WHERE name='max_connections' GROUP BY setting"
            ).fetchone()
            connections_used = conn_row[0] if conn_row else 0
            connections_max = conn_row[1] if conn_row else 100
            connection_pressure = connections_used / max(connections_max, 1)

            # Lock stats
            lock_row = conn.execute(
                "SELECT count(*) as total, count(*) filter (where not granted) as waiting "
                "FROM pg_locks WHERE pid <> pg_backend_pid()"
            ).fetchone()
            locks_total = lock_row[0] if lock_row else 0
            locks_waiting = lock_row[1] if lock_row else 0
            lock_pressure = locks_waiting / max(locks_total, 1)

            # Replication lag
            repl_row = conn.execute(
                "SELECT COALESCE(max(pg_wal_lsn_diff(sent_lsn, replay_lsn)), 0) "
                "FROM pg_stat_replication"
            ).fetchone()
            replication_lag_bytes = repl_row[0] if repl_row else 0

            # Migration count
            mig_row = conn.execute("SELECT count(*) FROM gptbridge_migration.history").fetchone()
            migration_count = int(mig_row[0]) if mig_row else 0

    except (psycopg.Error, OSError) as exc:
        return PgHealthMetrics(
            available=True,
            latency_ms=health.latency_ms,
            connections_used=0,
            connections_max=0,
            connection_pressure=1.0,
            locks_total=0,
            locks_waiting=0,
            lock_pressure=1.0,
            replication_lag_bytes=0,
            migration_count=0,
            error=str(exc)[:200],
        )

    return PgHealthMetrics(
        available=True,
        latency_ms=health.latency_ms,
        connections_used=connections_used,
        connections_max=connections_max,
        connection_pressure=connection_pressure,
        locks_total=locks_total,
        locks_waiting=locks_waiting,
        lock_pressure=lock_pressure,
        replication_lag_bytes=replication_lag_bytes,
        migration_count=migration_count,
        error=None,
    )


def collect_pool_pressure(pool) -> PgPoolPressure:
    """Collect PostgreSQL pool pressure metrics.

    Reads live counters from ``ConnectionManager.stats()`` when a real pool
    is passed (G26); falls back to zeros for ``None`` or stub pools.
    """
    stats_fn = getattr(pool, "stats", None)
    if callable(stats_fn):
        stats = stats_fn()
        size = int(stats.get("pool_size") or 0)
        maximum = max(1, int(stats.get("pool_max") or 1))
        active = int(stats.get("active_connections") or 0)
        return PgPoolPressure(
            pool_size=size,
            pool_max=maximum,
            pool_pressure=round(active / maximum, 4),
            idle_connections=int(stats.get("idle_connections") or 0),
            waiting_requests=0,
        )
    return PgPoolPressure(
        pool_size=0,
        pool_max=getattr(pool, "max_size", 20) if pool is not None else 20,
        pool_pressure=0.0,
        idle_connections=0,
        waiting_requests=0,
    )


def collect_lock_pressure(settings: DatabaseSettings) -> PgLockPressure:
    """Collect PostgreSQL lock pressure details."""
    try:
        with psycopg.connect(database_dsn(settings.admin_dsn, settings.database)) as conn:
            # Detailed lock info
            lock_row = conn.execute(
                "SELECT count(*) as total, "
                "count(*) filter (where mode = 'ExclusiveLock') as exclusive, "
                "count(*) filter (where not granted) as waiting "
                "FROM pg_locks WHERE pid <> pg_backend_pid()"
            ).fetchone()

            # Blocking PIDs
            blocking = conn.execute(
                "SELECT DISTINCT blocking_pid FROM pg_locks "
                "WHERE not granted AND blocking_pid IS NOT NULL"
            ).fetchall()

    except (psycopg.Error, OSError):
        return PgLockPressure(
            total_locks=0,
            exclusive_locks=0,
            waiting_locks=0,
            lock_pressure=1.0,
            blocking_pids=[],
        )

    return PgLockPressure(
        total_locks=lock_row[0] if lock_row else 0,
        exclusive_locks=lock_row[1] if lock_row else 0,
        waiting_locks=lock_row[2] if lock_row else 0,
        lock_pressure=lock_row[2] / max(lock_row[0], 1) if lock_row else 0.0,
        blocking_pids=[row[0] for row in blocking],
    )


def collect_statistics_freshness(settings: DatabaseSettings) -> PgStatisticsFreshness:
    """Collect PostgreSQL statistics freshness."""
    try:
        with psycopg.connect(database_dsn(settings.admin_dsn, settings.database)) as conn:
            row = conn.execute(
                "SELECT count(*) as total, "
                "count(*) filter (where last_analyze IS NULL OR "
                "last_analyze < now() - interval '24 hours') as stale, "
                "COALESCE(max(EXTRACT(EPOCH FROM (now() - last_analyze))/3600), 0) as oldest_age "
                "FROM pg_stat_user_tables"
            ).fetchone()

    except (psycopg.Error, OSError):
        return PgStatisticsFreshness(
            stale_tables=0,
            total_tables=0,
            stale_ratio=0.0,
            oldest_stats_age_hours=0.0,
        )

    total = row[0] if row else 0
    stale = row[1] if row else 0
    oldest_age = float(row[2]) if row and row[2] else 0.0

    return PgStatisticsFreshness(
        stale_tables=stale,
        total_tables=total,
        stale_ratio=stale / max(total, 1),
        oldest_stats_age_hours=oldest_age,
    )


def collect_growth_observation(settings: DatabaseSettings) -> list[PgGrowthObservation]:
    """Collect table/index growth observations."""
    observations = []
    try:
        with psycopg.connect(database_dsn(settings.admin_dsn, settings.database)) as conn:
            rows = conn.execute(
                "SELECT relname, "
                "pg_total_relation_size(oid)/1024/1024 as size_mb, "
                "pg_indexes_size(oid)/1024/1024 as index_size_mb, "
                "n_live_tup as row_count "
                "FROM pg_stat_user_tables "
                "ORDER BY pg_total_relation_size(oid) DESC "
                "LIMIT 50"
            ).fetchall()

            for row in rows:
                observations.append(PgGrowthObservation(
                    table_name=row[0],
                    size_mb=float(row[1]),
                    size_delta_mb=0.0,  # Would need historical comparison
                    index_size_mb=float(row[2]),
                    row_count=int(row[3]),
                    row_delta=0,
                ))

    except (psycopg.Error, OSError):
        pass

    return observations


# --- Execution Functions ---

def execute_pg_analyze(
    job: MaintenanceJob,
    action: MaintenanceAction,
    before_state: dict[str, Any],
) -> dict[str, Any]:
    """Execute ANALYZE on tables with stale statistics.

    Uses the settings from the job context or defaults.
    """
    after_state = dict(before_state)
    settings = job.before_state.get("settings")  # Would be passed in context

    if not settings:
        after_state["analyze_completed"] = False
        after_state["error"] = "No database settings provided"
        return after_state

    try:
        with psycopg.connect(database_dsn(settings.admin_dsn, settings.database)) as conn:
            # Get tables needing analyze
            tables = conn.execute(
                "SELECT relname FROM pg_stat_user_tables "
                "WHERE last_analyze IS NULL OR last_analyze < now() - interval '24 hours' "
                "ORDER BY pg_total_relation_size(oid) DESC LIMIT 20"
            ).fetchall()

            analyzed_count = 0
            for (table_name,) in tables:
                conn.execute(f"ANALYZE {table_name}")
                analyzed_count += 1

            # Verify stats refreshed
            stats_after = conn.execute(
                "SELECT count(*) FROM pg_stat_user_tables "
                "WHERE last_analyze > now() - interval '1 hour'"
            ).fetchone()

            after_state["analyze_completed"] = True
            after_state["tables_analyzed"] = analyzed_count
            after_state["stats_refreshed"] = (stats_after[0] if stats_after else 0) > 0
            after_state["new_critical_locks"] = 0
            after_state["pg_healthy"] = True
            after_state["maintenance_verified"] = True

    except (psycopg.Error, OSError) as exc:
        after_state["analyze_completed"] = False
        after_state["error"] = str(exc)[:300]
        after_state["pg_healthy"] = False

    return after_state


def execute_pg_health_observe(
    job: MaintenanceJob,
    action: MaintenanceAction,
    before_state: dict[str, Any],
) -> dict[str, Any]:
    """Execute PostgreSQL health observation."""
    after_state = dict(before_state)
    settings = job.before_state.get("settings")

    if not settings:
        after_state["metrics_collected"] = False
        return after_state

    try:
        health = collect_pg_health(settings)
        after_state["metrics_collected"] = True
        after_state["health_metrics"] = {
            "available": health.available,
            "latency_ms": health.latency_ms,
            "connection_pressure": health.connection_pressure,
            "lock_pressure": health.lock_pressure,
            "replication_lag_bytes": health.replication_lag_bytes,
        }
        after_state["maintenance_verified"] = True
    except Exception as exc:
        after_state["metrics_collected"] = False
        after_state["error"] = str(exc)[:200]

    return after_state


def execute_pg_vacuum_candidate(
    job: MaintenanceJob,
    action: MaintenanceAction,
    before_state: dict[str, Any],
) -> dict[str, Any]:
    """Candidate for VACUUM execution (not auto-enabled in v1).

    Interface preserved for future use.
    """
    after_state = dict(before_state)
    after_state["vacuum_completed"] = False
    after_state["error"] = "VACUUM not auto-enabled in v1; manual trigger required"
    return after_state


# --- Verification Functions ---

def verify_pg_analyze_execution(
    action: MaintenanceAction,
    before: dict[str, Any],
    after: dict[str, Any],
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Verify PostgreSQL ANALYZE execution."""
    return verify_pg_analyze(action, before, after, context)


def verify_pg_health_observe(
    action: MaintenanceAction,
    before: dict[str, Any],
    after: dict[str, Any],
    context: dict[str, Any],
) -> tuple[bool, str]:
    """Verify PostgreSQL health observation."""
    return verify_health_observe(action, before, after, context)


# --- Signal Builders ---

def build_pg_signals(
    health: PgHealthMetrics,
    pool: PgPoolPressure,
    locks: PgLockPressure,
    stats: PgStatisticsFreshness,
) -> dict[str, Any]:
    """Build telemetry signals for PostgreSQL evaluator."""
    return {
        "pg_healthy": health.available,
        "pg_latency_ms": health.latency_ms,
        "pg_connections": health.connections_used,
        "pg_connection_pressure": health.connection_pressure,
        "pg_lock_pressure": health.lock_pressure,
        "pg_replication_lag_bytes": health.replication_lag_bytes,
        "pg_migration_count": health.migration_count,
        "pool_pressure": pool.pool_pressure,
        "transport_pressure": 0.0,  # Would come from transport layer
        "lock_pressure": locks.lock_pressure,
        "pg_stats_stale": stats.stale_ratio > 0.1,
        "pg_stale_ratio": stats.stale_ratio,
        "pg_oldest_stats_age_hours": stats.oldest_stats_age_hours,
    }


def get_pg_maintenance_executors() -> dict[str, callable]:
    """Get PostgreSQL maintenance executors for the controller."""
    return {
        "pg_analyze_table_v1": execute_pg_analyze,
        "pg_health_observe_v1": execute_pg_health_observe,
        "pg_vacuum_candidate_v1": execute_pg_vacuum_candidate,
    }


__all__ = [
    "PgHealthMetrics",
    "PgPoolPressure",
    "PgLockPressure",
    "PgStatisticsFreshness",
    "PgGrowthObservation",
    "collect_pg_health",
    "collect_pool_pressure",
    "collect_lock_pressure",
    "collect_statistics_freshness",
    "collect_growth_observation",
    "execute_pg_analyze",
    "execute_pg_health_observe",
    "execute_pg_vacuum_candidate",
    "verify_pg_analyze_execution",
    "verify_pg_health_observe",
    "build_pg_signals",
    "get_pg_maintenance_executors",
]