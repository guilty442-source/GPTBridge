"""Reconciliation Service — SQLite ↔ PostgreSQL reconciliation (A207, A216, A367).

Part of A367 binding dependency order Phase 1: RECONCILIATION.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

import psycopg
from .reconciliation_helpers import ReconcileHelpersMixin


@dataclass(frozen=True)
class ReconcileResult:
    """Result of reconciling a single resource."""
    module_id: str
    resource_id: str
    action: str  # 'pushed', 'pulled', 'in-sync', 'conflict', 'skipped'
    local_version: int
    central_version: int | None
    detail: str = ""


class ReconcileService(ReconcileHelpersMixin):
    """One-directional SQLite → PostgreSQL reconciliation (A207, A216).

    When PostgreSQL recovers from downtime, local SQLite stores may have
    accumulated changes that need to be pushed to the central index.
    """

    def __init__(
        self,
        sqlite_connection: sqlite3.Connection,
        pg_connection: Any | None = None,
    ) -> None:
        self.sqlite = sqlite_connection
        self.pg = pg_connection
        self._ensure_reconcile_schema()

    def _ensure_reconcile_schema(self) -> None:
        """Ensure the SQLite database has the reconcile_state table."""
        self.sqlite.execute(
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
        self.sqlite.execute(
            "CREATE INDEX IF NOT EXISTS reconcile_state_pending_idx "
            "ON reconcile_state (reconcile_status, local_updated_at) "
            "WHERE reconcile_status = 'pending'"
        )
        self.sqlite.commit()

    def pending_count(self, module_id: str | None = None) -> int:
        """Count pending reconciliation entries."""
        if module_id:
            row = self.sqlite.execute(
                "SELECT COUNT(*) FROM reconcile_state WHERE module_id = ? AND reconcile_status = 'pending'",
                (module_id,),
            ).fetchone()
        else:
            row = self.sqlite.execute(
                "SELECT COUNT(*) FROM reconcile_state WHERE reconcile_status = 'pending'"
            ).fetchone()
        return int(row[0]) if row else 0

    def reconcile_module(
        self,
        module_id: str,
        *,
        batch_size: int = 100,
    ) -> Iterator[ReconcileResult]:
        """Reconcile all pending changes for a module.

        Yields ReconcileResult for each processed resource.
        """
        if self.pg is None:
            yield from self._reconcile_pg_unavailable(module_id, batch_size)
            return

        rows = self._fetch_pending_rows(module_id, batch_size)
        central_map = self._fetch_central_map(module_id, rows)

        for row in rows:
            yield from self._process_row(module_id, row, central_map)

    def _reconcile_pg_unavailable(
        self,
        module_id: str,
        batch_size: int,
    ) -> Iterator[ReconcileResult]:
        """Handle reconciliation when PostgreSQL is unavailable."""
        rows = self.sqlite.execute(
            """SELECT resource_id, local_version, local_updated_at, local_content_hash
               FROM reconcile_state
               WHERE module_id = ? AND reconcile_status = 'pending'
               ORDER BY local_updated_at ASC
               LIMIT ?""",
            (module_id, batch_size),
        ).fetchall()
        for row in rows:
            yield ReconcileResult(
                module_id=module_id,
                resource_id=str(row[0]),
                action="skipped",
                local_version=int(row[1]),
                central_version=None,
                detail="postgresql-unavailable",
            )

    def _fetch_pending_rows(
        self,
        module_id: str,
        batch_size: int,
    ) -> list[tuple]:
        """Fetch pending rows from SQLite."""
        return self.sqlite.execute(
            """SELECT resource_id, local_version, local_updated_at, local_content_hash
               FROM reconcile_state
               WHERE module_id = ? AND reconcile_status = 'pending'
               ORDER BY local_updated_at ASC
               LIMIT ?""",
            (module_id, batch_size),
        ).fetchall()

    def _process_row(
        self,
        module_id: str,
        row: tuple,
        central_map: dict[str, dict[str, Any] | None],
    ) -> Iterator[ReconcileResult]:
        """Process a single row and yield reconciliation results."""
        resource_id = str(row[0])
        local_version = int(row[1])
        local_updated_at = str(row[2])
        local_hash = str(row[3]) if row[3] else None

        central = central_map.get(resource_id)
        now = datetime.now(timezone.utc).isoformat()

        if central is None:
            yield from self._handle_new_resource(module_id, resource_id, local_version, local_hash, now)
        elif central["version"] < local_version:
            yield from self._handle_local_ahead(module_id, resource_id, local_version, local_hash, central, now)
        elif central["version"] > local_version:
            yield from self._handle_central_ahead(module_id, resource_id, local_version, central, now)
        elif central.get("content_hash") != local_hash:
            yield from self._handle_conflict(module_id, resource_id, local_version, central, now)
        else:
            yield from self._handle_in_sync(module_id, resource_id, local_version, central, now)

    def _handle_new_resource(
        self,
        module_id: str,
        resource_id: str,
        local_version: int,
        local_hash: str | None,
        now: str,
    ) -> Iterator[ReconcileResult]:
        self._push_to_central(module_id, resource_id, local_version, local_hash)
        self._mark_reconciled(module_id, resource_id, now, "in-sync")
        yield ReconcileResult(
            module_id=module_id,
            resource_id=resource_id,
            action="pushed",
            local_version=local_version,
            central_version=None,
        )

    def _handle_local_ahead(
        self,
        module_id: str,
        resource_id: str,
        local_version: int,
        local_hash: str | None,
        central: dict[str, Any],
        now: str,
    ) -> Iterator[ReconcileResult]:
        self._push_to_central(module_id, resource_id, local_version, local_hash)
        self._mark_reconciled(module_id, resource_id, now, "in-sync")
        yield ReconcileResult(
            module_id=module_id,
            resource_id=resource_id,
            action="pushed",
            local_version=local_version,
            central_version=int(central["version"]),
        )

    def _handle_central_ahead(
        self,
        module_id: str,
        resource_id: str,
        local_version: int,
        central: dict[str, Any],
        now: str,
    ) -> Iterator[ReconcileResult]:
        self._pull_from_central(module_id, resource_id)
        self._mark_reconciled(module_id, resource_id, now, "in-sync")
        yield ReconcileResult(
            module_id=module_id,
            resource_id=resource_id,
            action="pulled",
            local_version=local_version,
            central_version=int(central["version"]),
        )

    def _handle_conflict(
        self,
        module_id: str,
        resource_id: str,
        local_version: int,
        central: dict[str, Any],
        now: str,
    ) -> Iterator[ReconcileResult]:
        self._mark_reconciled(module_id, resource_id, now, "conflict")
        yield ReconcileResult(
            module_id=module_id,
            resource_id=resource_id,
            action="conflict",
            local_version=local_version,
            central_version=int(central["version"]),
            detail="version-match-hash-mismatch",
        )

    def _handle_in_sync(
        self,
        module_id: str,
        resource_id: str,
        local_version: int,
        central: dict[str, Any],
        now: str,
    ) -> Iterator[ReconcileResult]:
        self._mark_reconciled(module_id, resource_id, now, "in-sync")
        yield ReconcileResult(
            module_id=module_id,
            resource_id=resource_id,
            action="in-sync",
            local_version=local_version,
            central_version=int(central["version"]),
        )

    def _push_to_central(
        self,
        module_id: str,
        resource_id: str,
        version: int,
        content_hash: str | None,
    ) -> None:
        """Push a local resource to the central PostgreSQL index."""
        now_iso = datetime.now(timezone.utc).isoformat()
        self.pg.execute(
            """INSERT INTO gptbridge_index.resource
                (module_id, resource_id, version, content_hash, updated_at, status)
               VALUES (%s, %s, %s, %s, %s, 'active')
               ON CONFLICT (module_id, resource_id) DO UPDATE SET
                 version = excluded.version,
                 content_hash = excluded.content_hash,
                 updated_at = excluded.updated_at,
                 status = 'active'""",
            (module_id, resource_id, version, content_hash, now_iso),
        )
        self.pg.commit()

    def _pull_from_central(self, module_id: str, resource_id: str) -> None:
        """Pull central version back to local SQLite (central wins)."""
        cur = self.pg.execute(
            """SELECT version, content_hash, updated_at, status
               FROM gptbridge_index.resource
               WHERE module_id = %s AND resource_id = %s""",
            (module_id, resource_id),
        )
        row = cur.fetchone()
        if row is None:
            return
        self.sqlite.execute(
            """UPDATE resource_metadata SET
                 version = ?, content_hash = ?, updated_at = ?, status = ?
               WHERE module_id = ? AND resource_id = ?""",
            (int(row[0]), str(row[1]) if row[1] else None,
             str(row[2]), str(row[3]),
             module_id, resource_id),
        )
        self.sqlite.commit()

    def _mark_reconciled(
        self,
        module_id: str,
        resource_id: str,
        timestamp: str,
        status: str,
    ) -> None:
        """Mark a resource as reconciled."""
        self.sqlite.execute(
            """UPDATE reconcile_state SET
                 reconcile_status = ?, reconciled_at = ?
               WHERE module_id = ? AND resource_id = ?""",
            (status, timestamp, module_id, resource_id),
        )
        self.sqlite.commit()


__all__ = ["ReconcileResult", "ReconcileService"]