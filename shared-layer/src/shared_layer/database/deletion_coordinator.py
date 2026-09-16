"""Deletion Coordinator (migration 027 + C6).

Manages the two-stage deletion lifecycle:
    active → tombstone → retention window → purge

The coordinator tombstones a resource, waits for the retention window
to expire, then purges it from all three engines (PostgreSQL index,
SQLite private data, Qdrant vectors) in sync.

Usage:
    from shared_layer.database.deletion_coordinator import (
        tombstone,
        advance_stage,
        get_purge_eligible,
    )

    with connection_manager.connection() as conn:
        tombstone(conn, resource_id="res-123", retention_days=30)

    # Later (scheduled job):
    with connection_manager.connection() as conn:
        eligible = get_purge_eligible(conn)
        for item in eligible:
            # Delete from SQLite + Qdrant first, then advance
            advance_stage(conn, resource_id=item["resource_id"])

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A44/E30 — four-functions-local.
    A52/E38 — RAG: Qdrant canonical semantic index.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg import Connection

_TOMBSTONE = "SELECT gptbridge_index.tombstone_resource(%s, %s)"
_ADVANCE = "SELECT gptbridge_index.advance_deletion_stage(%s)"
_GET_ELIGIBLE = "SELECT gptbridge_index.get_purge_eligible(%s)"


def tombstone(
    connection: Connection[Any],
    *,
    resource_id: str,
    retention_days: int = 30,
) -> None:
    """Mark a resource as tombstoned (stage: active → tombstone).

    The resource remains recoverable until the retention window expires.
    The runtime is responsible for syncing the tombstone to SQLite
    (pending-delete) and Qdrant (point tombstone).
    """
    connection.execute(_TOMBSTONE, (resource_id, retention_days))


def advance_stage(
    connection: Connection[Any],
    *,
    resource_id: str,
) -> str:
    """Advance the deletion stage (tombstone → retention → purged).

    Returns the new stage.  The caller must ensure cross-engine cleanup
    (SQLite, Qdrant) is complete before advancing to 'purged'.
    """
    row = connection.execute(_ADVANCE, (resource_id,)).fetchone()
    return str(row[0]) if row and row[0] else "unknown"


def get_purge_eligible(
    connection: Connection[Any],
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List resources that are past their retention window and ready to purge."""
    rows = connection.execute(_GET_ELIGIBLE, (limit,)).fetchall()
    return [
        {
            "resource_id": str(r[0]),
            "module_id": str(r[1]),
            "tombstoned_at": r[2],
            "purge_after": r[3],
        }
        for r in rows
    ]


__all__ = ["tombstone", "advance_stage", "get_purge_eligible"]
