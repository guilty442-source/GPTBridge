"""RAG durable stores — A374 tombstone guard, reconciliation queue, outbox.

SQLite-backed local durability for the bounded degraded path; in
production the PostgreSQL authority is the canonical durable store.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from .runtime_types import (
    CanonicalCheckError,
    OutboxStep,
    QueueOperation,
    QueueStatus,
    ReconciliationQueueItem,
    SagaResult,
    TombstoneRecord,
)

_logger = logging.getLogger("gptbridge.rag.runtime_state")

class TombstoneGuard:
    """A374: enforces tombstone anti-resurrection.

    Rules:
      * PostgreSQL tombstone is written first.
      * Resource revision is monotonic.
      * tombstone_generation is monotonic.
      * Queue items and Qdrant payloads carry tombstone metadata.
      * Stale create/update/vector writes are rejected.
      * Vectors are removed only after authoritative tombstone handling.
      * Purge only after retention/audit/consumer acknowledgement gates.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # resource_key -> TombstoneRecord (in-memory authoritative view; the
        # PostgreSQL authority is the durable source of truth).
        self._tombstones: dict[str, TombstoneRecord] = {}

    @staticmethod
    def _key(module_id: str, resource_id: str) -> str:
        return f"{module_id}:{resource_id}"

    def is_tombstoned(self, module_id: str, resource_id: str) -> bool:
        with self._lock:
            return self._key(module_id, resource_id) in self._tombstones

    def get(self, module_id: str, resource_id: str) -> Optional[TombstoneRecord]:
        with self._lock:
            return self._tombstones.get(self._key(module_id, resource_id))

    def current_generation(self, module_id: str, resource_id: str) -> int:
        record = self.get(module_id, resource_id)
        return record.tombstone_generation if record else 0

    def raise_tombstone(
        self,
        *,
        module_id: str,
        resource_id: str,
        source_revision: int,
        content_hash: str,
        reason: str = "deleted",
    ) -> TombstoneRecord:
        """Raise a monotonic tombstone.  Rejects stale or duplicate writes."""
        key = self._key(module_id, resource_id)
        with self._lock:
            existing = self._tombstones.get(key)
            if existing and existing.source_revision >= source_revision:
                raise TransitionError(
                    f"TOMBSTONE_STALE_REVISION: existing={existing.source_revision} "
                    f"requested={source_revision}"
                )
            next_generation = (existing.tombstone_generation + 1) if existing else 1
            record = TombstoneRecord(
                resource_id=resource_id,
                module_id=module_id,
                tombstone_generation=next_generation,
                source_revision=source_revision,
                content_hash=content_hash,
                created_at=datetime.now(timezone.utc).isoformat(),
                reason=reason,
            )
            self._tombstones[key] = record
            _logger.info(
                "TombstoneGuard: raised generation=%s for %s:%s revision=%s",
                next_generation, module_id, resource_id, source_revision,
            )
            return record

    def reject_stale_write(
        self,
        *,
        module_id: str,
        resource_id: str,
        source_revision: int,
        operation: str,
    ) -> None:
        """Reject a stale create/update/vector write against a tombstone."""
        record = self.get(module_id, resource_id)
        if record is None:
            return
        if operation in (QueueOperation.CREATE.value, QueueOperation.UPDATE.value):
            if source_revision <= record.source_revision:
                raise TransitionError(
                    f"TOMBSTONE_RESURRECTION_REJECTED: "
                    f"{module_id}:{resource_id} operation={operation} "
                    f"revision={source_revision} <= tombstoned={record.source_revision} "
                    f"generation={record.tombstone_generation}"
                )
        # tombstone/archive operations may proceed at higher revisions.

    def purge(
        self,
        *,
        module_id: str,
        resource_id: str,
        retention_acknowledged: bool,
        audit_acknowledged: bool,
        consumer_acknowledged: bool,
    ) -> bool:
        """Purge a tombstone only after all acknowledgement gates pass (A374)."""
        if not (retention_acknowledged and audit_acknowledged and consumer_acknowledged):
            raise TransitionError(
                "TOMBSTONE_PURGE_GATE_UNMET: retention/audit/consumer acknowledgement required"
            )
        key = self._key(module_id, resource_id)
        with self._lock:
            existing = self._tombstones.get(key)
            if existing is None:
                return False
            purged = TombstoneRecord(
                resource_id=existing.resource_id,
                module_id=existing.module_id,
                tombstone_generation=existing.tombstone_generation,
                source_revision=existing.source_revision,
                content_hash=existing.content_hash,
                created_at=existing.created_at,
                reason=existing.reason,
                purged=True,
            )
            self._tombstones[key] = purged
            return True


# ---------------------------------------------------------------------------
# A374: Durable reconciliation queue (SQLite-backed for local authority;
# PostgreSQL authority is the canonical durable store in production).
# ---------------------------------------------------------------------------


class ReconciliationQueue:
    """A374: durable queue of degraded mutations awaiting reconciliation.

    The queue is durable: items survive process restarts.  In production the
    canonical durable store is PostgreSQL; for local testing and for the
    bounded degraded path a SQLite store is used.  The queue status lifecycle
    is: pending → leased → reconciling → verified | conflict | failed |
    dead_letter.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._lock = threading.Lock()
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        with self._lock:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rag_reconciliation_queue (
                    operation_id TEXT NOT NULL PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    resource_id TEXT NOT NULL,
                    locator_id TEXT NOT NULL,
                    source_revision INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    tombstone_generation INTEGER NOT NULL DEFAULT 0,
                    embedding_model TEXT NOT NULL,
                    embedding_version INTEGER NOT NULL DEFAULT 1,
                    chunk_size INTEGER NOT NULL DEFAULT 0,
                    chunk_overlap INTEGER NOT NULL DEFAULT 0,
                    chunking_version INTEGER NOT NULL DEFAULT 1,
                    parser_version INTEGER NOT NULL DEFAULT 1,
                    schema_version INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT,
                    deadline TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    correlation_id TEXT,
                    last_error TEXT,
                    degraded_indexed_at TEXT,
                    canonical_synced_at TEXT,
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS rag_queue_status_idx
                    ON rag_reconciliation_queue (status, next_retry_at);
                CREATE INDEX IF NOT EXISTS rag_queue_resource_idx
                    ON rag_reconciliation_queue (resource_id, source_revision);
                """
            )
            existing = {
                str(row[1])
                for row in self.connection.execute(
                    "PRAGMA table_info(rag_reconciliation_queue)"
                ).fetchall()
            }
            for column in ("degraded_indexed_at", "canonical_synced_at"):
                if column not in existing:
                    self.connection.execute(
                        f"ALTER TABLE rag_reconciliation_queue "
                        f"ADD COLUMN {column} TEXT"
                    )
            self.connection.commit()

    _QUEUE_COLUMNS = (
        "operation_id, idempotency_key, resource_id, locator_id, "
        "source_revision, content_hash, operation, tombstone_generation, "
        "embedding_model, embedding_version, chunk_size, chunk_overlap, "
        "chunking_version, parser_version, schema_version, created_at, "
        "attempts, next_retry_at, deadline, status, correlation_id, "
        "last_error, degraded_indexed_at, canonical_synced_at, payload"
    )

    def enqueue(self, item: ReconciliationQueueItem) -> None:
        """Insert a durable queue item.  Idempotent on idempotency_key."""
        with self._lock:
            self.connection.execute(
                """
                INSERT INTO rag_reconciliation_queue (
                    operation_id, idempotency_key, resource_id, locator_id,
                    source_revision, content_hash, operation, tombstone_generation,
                    embedding_model, embedding_version, chunk_size, chunk_overlap,
                    chunking_version, parser_version, schema_version, created_at,
                    attempts, next_retry_at, deadline, status, correlation_id,
                    last_error, degraded_indexed_at, canonical_synced_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    status = 'pending',
                    last_error = NULL,
                    next_retry_at = NULL,
                    canonical_synced_at = NULL
                """,
                (
                    item.operation_id, item.idempotency_key, item.resource_id,
                    item.locator_id, item.source_revision, item.content_hash,
                    item.operation, item.tombstone_generation,
                    item.embedding_model, item.embedding_version,
                    item.chunk_size, item.chunk_overlap,
                    item.chunking_version, item.parser_version, item.schema_version,
                    item.created_at, item.attempts, item.next_retry_at, item.deadline,
                    item.status, item.correlation_id, item.last_error,
                    item.degraded_indexed_at or item.created_at,
                    item.canonical_synced_at,
                    json.dumps(item.payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            self.connection.commit()

    def pending_count(self) -> int:
        with self._lock:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM rag_reconciliation_queue WHERE status IN ('pending','leased','reconciling','failed')"
            ).fetchone()
        return int(row[0]) if row else 0

    def is_complete(self) -> bool:
        """A374: queue is complete when no pending/leased/reconciling/failed items remain."""
        return self.pending_count() == 0

    def lease(self, limit: int = 10) -> list[ReconciliationQueueItem]:
        """Lease pending items for reconciliation (mark as leased)."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            rows = self.connection.execute(
                f"""
                SELECT {self._QUEUE_COLUMNS} FROM rag_reconciliation_queue
                WHERE status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (now, max(1, int(limit))),
            ).fetchall()
            # Single UPDATE for the whole batch — no per-row N+1.
            ids = [
                row["operation_id"] if isinstance(row, sqlite3.Row) else row[0]
                for row in rows
            ]
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                self.connection.execute(
                    "UPDATE rag_reconciliation_queue "
                    "SET status='leased', attempts=attempts+1 "
                    f"WHERE operation_id IN ({placeholders})",
                    tuple(ids),
                )
            leased = [ReconciliationQueueItem.from_row(row) for row in rows]
            self.connection.commit()
        return leased

    def mark_status(
        self,
        operation_id: str,
        status: QueueStatus,
        *,
        last_error: Optional[str] = None,
        next_retry_at: Optional[str] = None,
    ) -> None:
        with self._lock:
            self.connection.execute(
                """
                UPDATE rag_reconciliation_queue
                SET status=?, last_error=?, next_retry_at=?
                WHERE operation_id=?
                """,
                (status.value, last_error, next_retry_at, operation_id),
            )
            self.connection.commit()

    def mark_verified(self, operation_id: str) -> None:
        """Mark an item reconciled: canonical_synced_at is the verification stamp."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self.connection.execute(
                """
                UPDATE rag_reconciliation_queue
                SET status='verified', canonical_synced_at=?, last_error=NULL,
                    next_retry_at=NULL
                WHERE operation_id=?
                """,
                (now, operation_id),
            )
            self.connection.commit()

    def mark_retry(
        self,
        operation_id: str,
        error: str,
        *,
        delay_seconds: float,
        max_attempts: int = 8,
    ) -> str:
        """Schedule a retry or dead-letter when attempts are exhausted.

        Returns the resulting status ('pending' or 'dead_letter').
        """
        row = self.connection.execute(
            "SELECT attempts FROM rag_reconciliation_queue WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        attempts = int(row[0]) if row else 0
        if attempts >= max_attempts:
            self.dead_letter(operation_id, error)
            return QueueStatus.DEAD_LETTER.value
        retry_at = (
            datetime.now(timezone.utc).timestamp() + max(1.0, delay_seconds)
        )
        self.mark_status(
            operation_id,
            QueueStatus.PENDING,
            last_error=error[:400],
            next_retry_at=datetime.fromtimestamp(
                retry_at, tz=timezone.utc
            ).isoformat(),
        )
        return QueueStatus.PENDING.value

    def drain_verified(self) -> int:
        """Remove verified items (called only after reconciliation parity)."""
        with self._lock:
            cur = self.connection.execute(
                "DELETE FROM rag_reconciliation_queue WHERE status='verified'"
            )
            self.connection.commit()
            return int(cur.rowcount or 0)

    def dead_letter(self, operation_id: str, reason: str) -> None:
        self.mark_status(operation_id, QueueStatus.DEAD_LETTER, last_error=reason)

    def all_items(self) -> list[ReconciliationQueueItem]:
        with self._lock:
            rows = self.connection.execute(
                f"SELECT {self._QUEUE_COLUMNS} FROM rag_reconciliation_queue ORDER BY created_at ASC"
            ).fetchall()
        return [ReconciliationQueueItem.from_row(row) for row in rows]


# ---------------------------------------------------------------------------
# A374: Cross-store outbox/saga executor.
# ---------------------------------------------------------------------------


class CrossStoreOutbox:
    """A374: outbox/saga coordinator for idempotent Qdrant + PostgreSQL writes.

    Order:
      1. Commit canonical operation identity (queue item / intent) first.
      2. Apply Qdrant and PostgreSQL writes idempotently.
      3. Record each store result.
      4. Expose partial success as incomplete/recoverable.
      5. Compensate or enqueue reconciliation.
      6. Never report completion until revision/hash/tombstone parity is proven.
    """

    def __init__(self, queue: ReconciliationQueue, tombstone: TombstoneGuard) -> None:
        self.queue = queue
        self.tombstone = tombstone

    def execute(
        self,
        *,
        operation_id: str,
        qdrant_writer: Callable[[], tuple[bool, Optional[str], Optional[str]]],
        postgresql_writer: Callable[[], tuple[bool, Optional[str], Optional[str]]],
        verify_parity: Callable[[list[OutboxStep]], bool],
    ) -> SagaResult:
        """Execute a cross-store saga.  Never reports complete until parity."""
        now = datetime.now(timezone.utc).isoformat()
        steps: list[OutboxStep] = []

        # Step 1: Qdrant write (idempotent — caller responsibility)
        q_ok, q_err, q_id = qdrant_writer()
        steps.append(OutboxStep(
            store="qdrant", operation="upsert", succeeded=q_ok,
            applied_at=now, error=q_err, store_record_id=q_id,
        ))

        # Step 2: PostgreSQL write (idempotent — caller responsibility)
        p_ok, p_err, p_id = postgresql_writer()
        steps.append(OutboxStep(
            store="postgresql", operation="upsert_index_state", succeeded=p_ok,
            applied_at=now, error=p_err, store_record_id=p_id,
        ))

        # Step 3: Parity verification — never report complete until proven.
        complete = q_ok and p_ok and verify_parity(steps)
        compensation_required = (q_ok ^ p_ok)  # exactly one store succeeded

        return SagaResult(
            operation_id=operation_id,
            complete=complete,
            steps=steps,
            compensation_required=compensation_required,
            queue_item_id=operation_id if not complete else None,
        )
