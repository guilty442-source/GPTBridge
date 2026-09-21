"""Watchdog and Bloat Collector (migration 029 + D4).

Monitors long-running transactions, idle-in-transaction sessions, lock
holders, table/index bloat, dead tuples, and autovacuum lag.

Usage:
    from shared_layer.database.watchdog import (
        check_long_transactions,
        collect_bloat_report,
        get_rpo_rto_classes,
        get_capacity_thresholds,
    )

    with connection_manager.connection() as conn:
        long_txs = check_long_transactions(conn, threshold_seconds=300)
        bloat = collect_bloat_report(conn)
        rpo_rto = get_rpo_rto_classes(conn)
        thresholds = get_capacity_thresholds(conn)

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A46/E22 — Audit: mandatory-ledger.
"""
from __future__ import annotations

from typing import Any

from psycopg import Connection

# Long transaction query (pg_stat_activity)
_LONG_TX_QUERY = """
    SELECT pid, usename, state, query,
           EXTRACT(EPOCH FROM now() - xact_start)::bigint AS tx_age,
           EXTRACT(EPOCH FROM now() - state_change)::bigint AS idle_age,
           EXISTS (
               SELECT 1 FROM pg_locks WHERE pid = pg_stat_activity.pid
               AND granted AND mode != 'AccessShareLock'
           ) AS lock_holder
    FROM pg_stat_activity
    WHERE xact_start IS NOT NULL
      AND now() - xact_start > (%s || ' seconds')::interval
    ORDER BY xact_start
"""

_INSERT_WATCHDOG = (
    "INSERT INTO gptbridge_index.long_transaction_watchdog "
    "(pid, session_user, state, query, transaction_age_seconds, "
    "idle_in_transaction_seconds, lock_holder, threshold_seconds, action_taken) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

# Bloat estimation (pgstattuple is optional; use pg_stat_user_tables for basic)
_BLOAT_QUERY_TEMPLATE = """
    SELECT
        schemaname, relname,
        COALESCE(n_dead_tup, 0) AS dead_tuples,
        COALESCE(n_live_tup, 0) AS live_tuples,
        last_autovacuum, autovacuum_count,
        last_analyze,
        pg_total_relation_size(relid) AS table_size,
        pg_indexes_size(relid) AS index_size
    FROM pg_stat_user_tables
    WHERE schemaname = ANY(%s)
    ORDER BY pg_total_relation_size(relid) DESC
"""

# Default schemas for each strategy
_DEFAULT_SCHEMAS = (
    'gptbridge_index', 'gptbridge_rag', 'gptbridge_transport', 'gptbridge_audit'
)
_TRANSPORT_SCHEMAS = ('gptbridge_transport',)
_AUDIT_SCHEMAS = ('gptbridge_audit',)

_INSERT_BLOAT = (
    "INSERT INTO gptbridge_index.bloat_report "
    "(schema_name, table_name, dead_tuples, live_tuples, "
    "last_autovacuum, autovacuum_count, last_analyze, "
    "table_size_bytes, index_size_bytes) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)

_GET_RPO_RTO = (
    "SELECT engine, rpo_seconds, rto_seconds, backup_frequency_seconds, description "
    "FROM gptbridge_index.rpo_rto_class ORDER BY engine"
)

_GET_THRESHOLDS = (
    "SELECT metric_name, warning_level, critical_level, fail_closed_level, "
    "unit, description FROM gptbridge_index.capacity_threshold ORDER BY metric_name"
)


def check_long_transactions(
    connection: Connection[Any],
    *,
    threshold_seconds: int = 300,
    terminate: bool = False,
) -> list[dict[str, Any]]:
    """Detect long-running transactions and record them.

    Returns the list of detected long transactions.  Each is also
    inserted into long_transaction_watchdog.
    """
    rows = connection.execute(_LONG_TX_QUERY, (threshold_seconds,)).fetchall()
    results: list[dict[str, Any]] = []
    for r in rows:
        pid = int(r[0])
        session_user = str(r[1]) if r[1] else "unknown"
        state = str(r[2]) if r[2] else "unknown"
        query_text = str(r[3])[:1000] if r[3] else ""
        tx_age = int(r[4]) if r[4] else 0
        idle_age = int(r[5]) if r[5] else None
        lock_holder = bool(r[6])
        action = "logged"
        if terminate:
            try:
                connection.execute("SELECT pg_terminate_backend(%s)", (pid,))
                action = "terminated"
            except Exception:
                action = "terminate_failed"
        connection.execute(
            _INSERT_WATCHDOG,
            (pid, session_user, state, query_text, tx_age,
             idle_age, lock_holder, threshold_seconds, action),
        )
        results.append({
            "pid": pid,
            "session_user": session_user,
            "state": state,
            "transaction_age_seconds": tx_age,
            "idle_in_transaction_seconds": idle_age,
            "lock_holder": lock_holder,
            "action_taken": action,
        })
    return results


def terminate_long_transactions(
    connection: Connection[Any],
    *,
    threshold_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Terminate long-running transactions exceeding threshold.

    Returns list of terminated transactions with action status.
    """
    return check_long_transactions(connection, threshold_seconds=threshold_seconds, terminate=True)


def collect_bloat_report(
    connection: Connection[Any],
    *,
    schemas: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Collect table bloat/dead-tuple/autovacuum stats and record them.

    Args:
        schemas: Specific schemas to collect. If None, uses default all schemas.
                 Use 'transport' or 'audit' for independent strategies.
    """
    if schemas is None:
        schema_list = list(_DEFAULT_SCHEMAS)
    elif schemas == "transport":
        schema_list = list(_TRANSPORT_SCHEMAS)
    elif schemas == "audit":
        schema_list = list(_AUDIT_SCHEMAS)
    else:
        schema_list = list(schemas)

    rows = connection.execute(_BLOAT_QUERY_TEMPLATE, (schema_list,)).fetchall()
    results: list[dict[str, Any]] = []
    for r in rows:
        schema = str(r[0])
        table = str(r[1])
        dead = int(r[2])
        live = int(r[3])
        last_av = r[4]
        av_count = int(r[5]) if r[5] else 0
        last_an = r[6]
        table_size = int(r[7])
        index_size = int(r[8])
        connection.execute(
            _INSERT_BLOAT,
            (schema, table, dead, live, last_av, av_count, last_an,
             table_size, index_size),
        )
        results.append({
            "schema": schema,
            "table": table,
            "dead_tuples": dead,
            "live_tuples": live,
            "table_size_bytes": table_size,
            "index_size_bytes": index_size,
        })
    return results


def collect_bloat_transport(connection: Connection[Any]) -> list[dict[str, Any]]:
    """Collect bloat for transport schema with independent strategy."""
    return collect_bloat_report(connection, schemas="transport")


def collect_bloat_audit(connection: Connection[Any]) -> list[dict[str, Any]]:
    """Collect bloat for audit schema with independent strategy."""
    return collect_bloat_report(connection, schemas="audit")


def get_rpo_rto_classes(connection: Connection[Any]) -> list[dict[str, Any]]:
    """Get RPO/RTO classes for all engines."""
    rows = connection.execute(_GET_RPO_RTO).fetchall()
    return [
        {
            "engine": str(r[0]),
            "rpo_seconds": int(r[1]),
            "rto_seconds": int(r[2]),
            "backup_frequency_seconds": int(r[3]),
            "description": str(r[4]) if r[4] else "",
        }
        for r in rows
    ]


def get_capacity_thresholds(connection: Connection[Any]) -> list[dict[str, Any]]:
    """Get capacity thresholds for all metrics."""
    rows = connection.execute(_GET_THRESHOLDS).fetchall()
    return [
        {
            "metric_name": str(r[0]),
            "warning": float(r[1]),
            "critical": float(r[2]),
            "fail_closed": float(r[3]),
            "unit": str(r[4]),
            "description": str(r[5]) if r[5] else "",
        }
        for r in rows
    ]


__all__ = [
    "check_long_transactions",
    "terminate_long_transactions",
    "collect_bloat_report",
    "collect_bloat_transport",
    "collect_bloat_audit",
    "get_rpo_rto_classes",
    "get_capacity_thresholds",
]
