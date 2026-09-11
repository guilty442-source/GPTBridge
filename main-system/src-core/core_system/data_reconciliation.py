"""reconcile — one-directional SQLite → PostgreSQL reconciliation.

When PostgreSQL recovers from downtime, local SQLite stores may have
accumulated changes that need to be pushed to the central index.  This
module implements the governed reconciliation flow:

    1. PostgreSQL down → SQLite continues (degraded mode)
    2. PostgreSQL recovers → reconcile runs
    3. For each pending local change:
       a. Compare local version vs central version
       b. If local > central: push local to central (forward reconcile)
       c. If local < central: central wins (local is stale, pull central)
       d. If local == central but hash differs: conflict (flag for review)
    4. Mark reconciled rows as 'in-sync'

This is NOT bidirectional sync.  The direction is always:
    SQLite (local truth) → PostgreSQL (central index)

The only exception is when central version > local (central was updated
by another module while this module was disconnected).  In that case,
central wins and the local store is updated to match.

A44/E30 + A8/E21.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from shared_layer.metadata_contract import (
    FIELD_CONTENT_HASH,
    FIELD_MODULE_ID,
    FIELD_RESOURCE_ID,
    FIELD_STATUS,
    FIELD_UPDATED_AT,
    FIELD_VERSION,
    STATUS_ARCHIVED,
    STATUS_DELETED,
    STATUS_INDEXED,
    STATUS_PENDING,
    ResourceMetadata,
)


@dataclass(frozen=True)
class ReconcileResult:
    """Result of reconciling a single resource."""

    module_id: str
    resource_id: str
    action: str  # 'pushed', 'pulled', 'in-sync', 'conflict', 'skipped'
    local_version: int
    central_version: int | None
    detail: str = ""


class ReconcileService:
    """One-directional SQLite → PostgreSQL reconciliation.

    Usage:
        service = ReconcileService(sqlite_conn, pg_conn)
        results = list(service.reconcile_module("xingcheng"))
        for r in results:
            print(f"{r.resource_id}: {r.action}")
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
            # PostgreSQL still down — cannot reconcile
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
            return

        # PostgreSQL available — reconcile
        rows = self.sqlite.execute(
            """SELECT resource_id, local_version, local_updated_at, local_content_hash
               FROM reconcile_state
               WHERE module_id = ? AND reconcile_status = 'pending'
               ORDER BY local_updated_at ASC
               LIMIT ?""",
            (module_id, batch_size),
        ).fetchall()

        for row in rows:
            resource_id = str(row[0])
            local_version = int(row[1])
            local_updated_at = str(row[2])
            local_hash = str(row[3]) if row[3] else None

            central = self._fetch_central(module_id, resource_id)
            now = datetime.now(timezone.utc).isoformat()

            if central is None:
                # Central doesn't have this resource — push local
                self._push_to_central(module_id, resource_id, local_version, local_hash)
                self._mark_reconciled(module_id, resource_id, now, "in-sync")
                yield ReconcileResult(
                    module_id=module_id,
                    resource_id=resource_id,
                    action="pushed",
                    local_version=local_version,
                    central_version=None,
                )
            elif central["version"] < local_version:
                # Local is ahead — push to central
                self._push_to_central(module_id, resource_id, local_version, local_hash)
                self._mark_reconciled(module_id, resource_id, now, "in-sync")
                yield ReconcileResult(
                    module_id=module_id,
                    resource_id=resource_id,
                    action="pushed",
                    local_version=local_version,
                    central_version=int(central["version"]),
                )
            elif central["version"] > local_version:
                # Central is ahead — pull from central (central wins)
                self._pull_from_central(module_id, resource_id)
                self._mark_reconciled(module_id, resource_id, now, "in-sync")
                yield ReconcileResult(
                    module_id=module_id,
                    resource_id=resource_id,
                    action="pulled",
                    local_version=local_version,
                    central_version=int(central["version"]),
                )
            elif central["content_hash"] != local_hash:
                # Same version but different hash — conflict
                self._mark_reconciled(module_id, resource_id, now, "conflict")
                yield ReconcileResult(
                    module_id=module_id,
                    resource_id=resource_id,
                    action="conflict",
                    local_version=local_version,
                    central_version=int(central["version"]),
                    detail="version-match-hash-mismatch",
                )
            else:
                # Already in sync
                self._mark_reconciled(module_id, resource_id, now, "in-sync")
                yield ReconcileResult(
                    module_id=module_id,
                    resource_id=resource_id,
                    action="in-sync",
                    local_version=local_version,
                    central_version=int(central["version"]),
                )

    def _fetch_central(
        self, module_id: str, resource_id: str
    ) -> dict[str, Any] | None:
        """Fetch the central version of a resource from PostgreSQL."""
        try:
            cur = self.pg.execute(
                """SELECT version, content_hash, updated_at, status
                   FROM gptbridge_index.resource
                   WHERE module_id = %s AND resource_id = %s""",
                (module_id, resource_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return {
                "version": int(row[0]),
                "content_hash": str(row[1]) if row[1] else None,
                "updated_at": str(row[2]),
                "status": str(row[3]),
            }
        except Exception:
            return None

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
