"""Reconcile service helpers mixin (A185 split).

Contains the mark_pending and _fetch_central_map methods extracted from
ReconcileService.
"""
from __future__ import annotations

from typing import Any


class ReconcileHelpersMixin:
    """Reconcile helper methods."""

    sqlite: Any
    pg: Any

    def mark_pending(
        self,
        module_id: str,
        resource_id: str,
        version: int,
        updated_at: str,
        content_hash: str | None = None,
    ) -> None:
        """Mark a local resource change as pending reconciliation."""
        self.sqlite.execute(
            """INSERT INTO reconcile_state
                (module_id, resource_id, local_version, local_updated_at,
                 local_content_hash, reconcile_status)
              VALUES (?, ?, ?, ?, ?, 'pending')
              ON CONFLICT (module_id, resource_id) DO UPDATE SET
                local_version = excluded.local_version,
                local_updated_at = excluded.local_updated_at,
                local_content_hash = excluded.local_content_hash,
                reconcile_status = 'pending',
                reconciled_at = NULL""",
            (module_id, resource_id, version, updated_at, content_hash),
        )
        self.sqlite.commit()

    def _fetch_central_map(
        self,
        module_id: str,
        rows: list[tuple],
    ) -> dict[str, dict[str, Any] | None]:
        """Batch-fetch all central resources in one query (A207)."""
        resource_ids = [str(row[0]) for row in rows]
        central_map: dict[str, dict[str, Any] | None] = {}
        if not resource_ids:
            return central_map

        try:
            cur = self.pg.execute(
                """SELECT resource_id, version, content_hash, updated_at, status
                   FROM gptbridge_index.resource
                   WHERE module_id = %s AND resource_id = ANY(%s)""",
                (module_id, resource_ids),
            )
            for pg_row in cur.fetchall():
                central_map[str(pg_row[0])] = {
                    "version": int(pg_row[1]),
                    "content_hash": str(pg_row[2]) if pg_row[2] else None,
                    "updated_at": str(pg_row[3]),
                    "status": str(pg_row[4]),
                }
        except Exception:
            pass
        return central_map


__all__ = ["ReconcileHelpersMixin"]
