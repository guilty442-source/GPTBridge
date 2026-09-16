"""Permission Snapshot Helper (migration 021 + B2).

Captures the current permission/RLS decision state before an important
write, so future rule changes can still explain why the write was allowed.

The snapshot is recorded in PostgreSQL via the
``gptbridge_security.capture_permission_snapshot()`` function (migration 021).
This module is the runtime entry point — it calls that function and returns
the snapshot_id so the caller can link it to the audit event.

Usage:
    from shared_layer.database.permission_snapshot import capture_snapshot

    with connection_manager.connection() as conn:
        snapshot_id = capture_snapshot(
            conn,
            actor_id="user-abc",
            target_module="xingcheng",
            target_resource_id="res-123",
            target_classification="private",
        )
        # ... perform the write ...

Codex basis:
    A46/E22 — Audit: mandatory-ledger; write=governed-executor.
    A10/E10 — explicit-allowlist; deny-by-default.
"""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from psycopg import Connection

_QUERY = (
    "SELECT gptbridge_security.capture_permission_snapshot(%s, %s, %s, %s)"
)


def capture_snapshot(
    connection: Connection[Any],
    *,
    actor_id: str,
    target_module: str,
    target_resource_id: Optional[str] = None,
    target_classification: Optional[str] = None,
) -> Optional[UUID]:
    """Capture a permission snapshot at the current moment.

    Returns the snapshot_id (UUID) or None if the function is unavailable.
    The snapshot records the session user's roles, evaluated RLS policies,
    and the can_read/can_write/can_write_resource decisions.
    """
    try:
        row = connection.execute(
            _QUERY,
            (actor_id, target_module, target_resource_id, target_classification),
        ).fetchone()
        if row and row[0]:
            return UUID(str(row[0]))
    except Exception:
        pass
    return None


__all__ = ["capture_snapshot"]
