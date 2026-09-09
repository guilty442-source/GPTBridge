"""Information-layer persistence for reconciliation state.

This module stores and exposes typed pending-sync records only.  It does not
decide conflict winners or choose push/pull actions; those decisions belong to
the system data sub-sovereign.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class PendingReconcileRecord:
    module_id: str
    resource_id: str
    local_version: int
    local_updated_at: str
    local_content_hash: str | None


class ReconcileStateStore:
    """Transport-neutral storage used by an authorized reconciliation owner."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS reconcile_state (
                module_id TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                local_version INTEGER NOT NULL,
                local_updated_at TEXT NOT NULL,
                local_content_hash TEXT,
                reconcile_status TEXT NOT NULL DEFAULT 'pending',
                reconciled_at TEXT,
                PRIMARY KEY (module_id, resource_id)
            )"""
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS reconcile_state_pending_idx "
            "ON reconcile_state (reconcile_status, local_updated_at) "
            "WHERE reconcile_status = 'pending'"
        )
        self.connection.commit()

    def mark_pending(
        self,
        module_id: str,
        resource_id: str,
        version: int,
        updated_at: str,
        content_hash: str | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO reconcile_state
                (module_id, resource_id, local_version, local_updated_at,
                 local_content_hash, reconcile_status)
               VALUES (?, ?, ?, ?, ?, 'pending')
               ON CONFLICT (module_id, resource_id) DO UPDATE SET
                 local_version = excluded.local_version,
                 local_updated_at = excluded.local_updated_at,
                 local_content_hash = excluded.local_content_hash,
                 reconcile_status = 'pending', reconciled_at = NULL""",
            (module_id, resource_id, version, updated_at, content_hash),
        )
        self.connection.commit()

    def pending(self, module_id: str, limit: int = 100) -> list[PendingReconcileRecord]:
        rows = self.connection.execute(
            """SELECT module_id, resource_id, local_version, local_updated_at,
                      local_content_hash
               FROM reconcile_state
               WHERE module_id = ? AND reconcile_status = 'pending'
               ORDER BY local_updated_at ASC LIMIT ?""",
            (module_id, max(1, int(limit))),
        ).fetchall()
        return [PendingReconcileRecord(*row) for row in rows]

    def pending_count(self, module_id: str | None = None) -> int:
        if module_id:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM reconcile_state "
                "WHERE module_id = ? AND reconcile_status = 'pending'",
                (module_id,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM reconcile_state WHERE reconcile_status = 'pending'"
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_result(
        self, module_id: str, resource_id: str, status: str, reconciled_at: str
    ) -> None:
        if status not in {"in-sync", "conflict"}:
            raise ValueError("invalid reconcile status")
        self.connection.execute(
            """UPDATE reconcile_state SET reconcile_status = ?, reconciled_at = ?
               WHERE module_id = ? AND resource_id = ?""",
            (status, reconciled_at, module_id, resource_id),
        )
        self.connection.commit()


__all__ = ["PendingReconcileRecord", "ReconcileStateStore"]
