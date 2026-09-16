"""Data Lifecycle Manager (G14).

Provides runtime helpers for the unified data lifecycle state machine
and purge queue management.

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A46/E22 — Audit: mandatory-ledger.
"""
from __future__ import annotations

from typing import Any


def transition_state(
    conn: Any,
    entity_type: str,
    entity_id: str,
    new_state: str,
    transitioned_by: str,
    reason: str | None = None,
) -> None:
    """Transition an entity to a new lifecycle state.

    States: ACTIVE → STALE → SUPERSEDED → TOMBSTONED → ARCHIVED → PURGED
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.transition_lifecycle_state(%s, %s, %s, %s, %s)",
            (entity_type, entity_id, new_state, transitioned_by, reason),
        )
    conn.commit()


def get_state(conn: Any, entity_type: str, entity_id: str) -> str:
    """Get the current lifecycle state of an entity."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.get_lifecycle_state(%s, %s)",
            (entity_type, entity_id),
        )
        row = cur.fetchone()
    return row[0] if row else "ACTIVE"


def enqueue_purge(
    conn: Any,
    resource_id: str,
    reason: str,
    requested_by: str,
    retention_days: int = 30,
    module_id: str | None = None,
    entity_type: str = "resource",
) -> str:
    """Add a resource to the purge queue."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.enqueue_purge(%s, %s, %s, %s, %s, %s, NULL)",
            (resource_id, reason, requested_by, retention_days,
             module_id, entity_type),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def get_purge_eligible(conn: Any, limit: int = 100) -> list[dict[str, Any]]:
    """Get resources eligible for purge (past retention, no holds)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT queue_id, resource_id, module_id, entity_type, "
            "retention_until "
            "FROM gptbridge_index.get_purge_eligible(%s)",
            (limit,),
        )
        rows = cur.fetchall()
    return [
        {
            "queue_id": str(r[0]),
            "resource_id": r[1],
            "module_id": r[2],
            "entity_type": r[3],
            "retention_until": r[4],
        }
        for r in rows
    ]


def check_dependencies(
    conn: Any,
    resource_id: str,
    checked_by: str,
) -> dict[str, Any]:
    """Check if a resource has dependencies before purge."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT can_purge, dependency_count, details "
            "FROM gptbridge_index.check_resource_dependencies(%s, %s)",
            (resource_id, checked_by),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return {"can_purge": False, "dependency_count": 0, "details": {}}
    return {
        "can_purge": row[0],
        "dependency_count": row[1],
        "details": row[2] if row[2] else {},
    }


def record_purge(
    conn: Any,
    resource_id: str,
    actor: str,
    executor: str,
    deleted_from: str,
    previous_hash: str | None = None,
    purge_queue_id: str | None = None,
) -> str:
    """Record a permanent deletion in the purge audit log."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_purge(%s, %s, %s, %s, %s, NULL, NULL, %s, NULL)",
            (resource_id, actor, executor, deleted_from,
             previous_hash, purge_queue_id),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def place_hold(
    conn: Any,
    entity_type: str,
    entity_id: str,
    hold_reason: str,
    placed_by: str,
    description: str | None = None,
) -> str:
    """Place a retention hold on an entity."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.place_hold(%s, %s, %s, %s, %s, NULL)",
            (entity_type, entity_id, hold_reason, placed_by, description),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def release_hold(conn: Any, hold_id: str, released_by: str) -> None:
    """Release a retention hold."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.release_hold(%s, %s)",
            (hold_id, released_by),
        )
    conn.commit()


def has_active_hold(conn: Any, entity_type: str, entity_id: str) -> bool:
    """Check if an entity has an active retention hold."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.has_active_hold(%s, %s)",
            (entity_type, entity_id),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def register_archive(
    conn: Any,
    source_engine: str,
    source_table: str,
    time_range_start: Any,
    time_range_end: Any,
    record_count: int,
    schema_version: int,
    storage_locator: str,
    integrity_hash: str,
    storage_format: str = "csv",
    module_id: str | None = None,
    source_schema: str | None = None,
    description: str | None = None,
) -> str:
    """Register a new archive in the catalog."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.register_archive(%s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, NULL, %s)",
            (source_engine, source_table, time_range_start, time_range_end,
             record_count, schema_version, storage_locator, integrity_hash,
             storage_format, module_id, source_schema, description),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


__all__ = [
    "transition_state",
    "get_state",
    "enqueue_purge",
    "get_purge_eligible",
    "check_dependencies",
    "record_purge",
    "place_hold",
    "release_hold",
    "has_active_hold",
    "register_archive",
]
