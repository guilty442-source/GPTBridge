"""Information-layer persistence for reconciliation state.

This module stores and exposes typed pending-sync records only.  It does not
decide conflict winners or choose push/pull actions; those decisions belong to
the system data sub-sovereign.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable


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

    def mark_pending_batch(
        self,
        records: Iterable[tuple[str, str, int, str, str | None]],
    ) -> int:
        """Batch variant of ``mark_pending`` — one commit per call.

        ``records`` yields ``(module_id, resource_id, version, updated_at,
        content_hash)`` tuples.  Reconcile scanners that discover many
        pending resources in one pass should use this instead of calling
        ``mark_pending`` per row, which would fsync once per row and
        starve the transport.
        """
        rows = list(records)
        if not rows:
            return 0
        self.connection.executemany(
            """INSERT INTO reconcile_state
                (module_id, resource_id, local_version, local_updated_at,
                 local_content_hash, reconcile_status)
               VALUES (?, ?, ?, ?, ?, 'pending')
               ON CONFLICT (module_id, resource_id) DO UPDATE SET
                 local_version = excluded.local_version,
                 local_updated_at = excluded.local_updated_at,
                 local_content_hash = excluded.local_content_hash,
                 reconcile_status = 'pending', reconciled_at = NULL""",
            rows,
        )
        self.connection.commit()
        return len(rows)

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

    def mark_results(
        self, results: Iterable[tuple[str, str, str]], reconciled_at: str
    ) -> int:
        """Batch variant of ``mark_result`` — one commit per call.

        ``results`` yields ``(module_id, resource_id, status)`` tuples;
        every status must be ``in-sync`` or ``conflict``.  Reconcile
        workers drain in bounded batches, so batching the write is the
        honest throughput path — per-row commit would fsync once per
        row and starve the transport.
        """
        rows = list(results)
        for _, _, status in rows:
            if status not in {"in-sync", "conflict"}:
                raise ValueError("invalid reconcile status")
        self.connection.executemany(
            """UPDATE reconcile_state SET reconcile_status = ?, reconciled_at = ?
               WHERE module_id = ? AND resource_id = ?""",
            [(status, reconciled_at, module_id, resource_id)
             for module_id, resource_id, status in rows],
        )
        self.connection.commit()
        return len(rows)


__all__ = ["PendingReconcileRecord", "ReconcileStateStore"]
