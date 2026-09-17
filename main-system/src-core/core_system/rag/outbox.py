"""Transactional Outbox repository — canonical ``gptbridge_rag.outbox_event``.

The legacy ``rag_outbox`` table is REMOVED: there is exactly one RAG
outbox authority (RAG-08 schema in ``rag_metadata._SAGA_DDL``).  This
repository keeps the pre-Phase-2 class/API surface as a compat adapter
writing the canonical table; ``mark_done`` maps to SUCCEEDED and
``mark_failed`` maps to RETRY / DEAD_LETTER.  New code should use
``PostgreSQLMetadataAuthority`` outbox methods or
``CanonicalRagPipeline.process_outbox``.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

try:
    from shared_layer.database.lineage import record_qdrant_point
    LINEAGE_AVAILABLE = record_qdrant_point is not None
except ImportError:  # pragma: no cover — shared-layer not on this process path
    LINEAGE_AVAILABLE = False
    record_qdrant_point = None  # type: ignore[assignment]

_logger = logging.getLogger("gptbridge.rag.outbox")


class OutboxOperation(str, Enum):
    """Outbox operation types."""
    UPSERT = "UPSERT"          # Index/create resource
    DELETE = "DELETE"          # Remove resource
    REINDEX = "REINDEX"        # Full generation reindex
    UPDATE_METADATA = "UPDATE_METADATA"  # Metadata only


class RagOutboxState(str, Enum):
    """rag_outbox table lifecycle states (DONE/FAILED terminal set)."""
    PENDING = "PENDING"        # Waiting for worker
    PROCESSING = "PROCESSING"  # Worker picked up
    DONE = "DONE"              # Successfully applied to Qdrant
    FAILED = "FAILED"          # Max retries exceeded
    DEAD_LETTER = "DEAD_LETTER"  # Permanent failure, needs manual intervention


@dataclass(frozen=True)
class OutboxEvent:
    """Outbox event for RAG mutations."""
    event_id: str
    operation: OutboxOperation
    resource_id: str
    module_id: str
    generation_id: str
    content_hash: Optional[str] = None
    payload: Optional[dict[str, Any]] = None  # Full point data for UPSERT
    attempts: int = 0
    state: RagOutboxState = RagOutboxState.PENDING
    last_error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# Canonical DDL lives in rag_metadata._SAGA_DDL (single authority);
# kept as a marker for legacy importers — never creates rag_outbox.
OUTBOX_TABLE_SQL = """
-- rag_outbox removed; canonical outbox is gptbridge_rag.outbox_event
-- (see core_system.rag.rag_metadata._SAGA_DDL).
"""


class OutboxRepository:
    """PostgreSQL-backed outbox repository."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=False)
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def create_event(
        self,
        operation: OutboxOperation,
        resource_id: str,
        module_id: str,
        generation_id: str,
        content_hash: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> OutboxEvent:
        """Create outbox event within the current transaction.

        Must be called inside a PostgreSQL transaction that also updates
        the canonical metadata tables. The caller is responsible for commit/rollback.
        """
        conn = self._get_conn()
        event_id = uuid.uuid4()
        now = datetime.now(timezone.utc).isoformat()

        event = OutboxEvent(
            event_id=str(event_id),
            operation=operation,
            resource_id=resource_id,
            module_id=module_id,
            generation_id=generation_id,
            content_hash=content_hash,
            payload=payload,
            created_at=now,
            updated_at=now,
        )

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO gptbridge_rag.outbox_event
                (event_id, request_id, operation, module_id, resource_id,
                 source_version, content_hash, generation_id, payload,
                 state, attempt_count, last_error)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event.event_id,
                    f"legacy-{event.event_id}",
                    event.operation.value,
                    event.module_id,
                    event.resource_id,
                    0,
                    event.content_hash or "",
                    event.generation_id,
                    json.dumps(event.payload) if event.payload else None,
                    "PENDING",
                    event.attempts,
                    event.last_error,
                ),
            )
        return event

    def fetch_pending(self, limit: int = 100) -> list[OutboxEvent]:
        """Atomically claim PENDING / due-RETRY events as PROCESSING."""
        conn = self._get_conn()
        events = []
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.outbox_event
                SET state = 'PROCESSING', updated_at = now()
                WHERE event_id IN (
                    SELECT event_id FROM gptbridge_rag.outbox_event
                    WHERE state = 'PENDING'
                       OR (state = 'RETRY'
                           AND (next_retry_at IS NULL
                                OR next_retry_at <= now()))
                    ORDER BY created_at
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                RETURNING event_id, operation, resource_id, module_id,
                          generation_id, content_hash, payload,
                          attempt_count, state, last_error,
                          created_at, updated_at
                """,
                (limit,),
            )
            for row in cur.fetchall():
                events.append(OutboxEvent(
                    event_id=str(row["event_id"]),
                    operation=OutboxOperation(row["operation"]),
                    resource_id=row["resource_id"],
                    module_id=row["module_id"],
                    generation_id=row["generation_id"],
                    content_hash=row["content_hash"],
                    payload=row["payload"],
                    attempts=row["attempt_count"],
                    state=RagOutboxState.PROCESSING,
                    last_error=row["last_error"],
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                ))
        conn.commit()
        return events

    def mark_processing(self, event_id: str) -> bool:
        """Mark event PROCESSING (canonical state)."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.outbox_event
                SET state = 'PROCESSING', updated_at = now()
                WHERE event_id = %s AND state = 'PENDING'
                """,
                (event_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_succeeded(self, event_id: str) -> bool:
        """Canonical terminal: SUCCEEDED + completed_at."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.outbox_event
                SET state = 'SUCCEEDED', completed_at = now(),
                    updated_at = now()
                WHERE event_id = %s
                """,
                (event_id,),
            )
            conn.commit()
            return cur.rowcount > 0

    # Compat name for pre-Phase-2 callers (DONE == SUCCEEDED).
    mark_done = mark_succeeded

    def mark_retry(self, event_id: str, error: str,
                   delay_seconds: float = 2.0) -> bool:
        """Canonical retriable failure: RETRY + next_retry_at."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.outbox_event
                SET state = 'RETRY', last_error = %s,
                    attempt_count = attempt_count + 1,
                    next_retry_at = now() + (%s || ' seconds')::interval,
                    updated_at = now()
                WHERE event_id = %s
                """,
                (error, str(delay_seconds), event_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_dead_letter(self, event_id: str, error: str) -> bool:
        """Canonical terminal failure — observable, never silent."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.outbox_event
                SET state = 'DEAD_LETTER', last_error = %s,
                    completed_at = now(), updated_at = now()
                WHERE event_id = %s
                """,
                (error, event_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def mark_failed(self, event_id: str, error: str,
                    max_attempts: int = 5) -> bool:
        """RETRY until max_attempts, then DEAD_LETTER (canonical)."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempt_count FROM gptbridge_rag.outbox_event "
                "WHERE event_id = %s",
                (event_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            attempts = row["attempt_count"] + 1
        if attempts >= max_attempts:
            return self.mark_dead_letter(event_id, error)
        return self.mark_retry(event_id, error)

    def get_stats(self) -> dict[str, int]:
        """Canonical outbox statistics by state."""
        conn = self._get_conn()
        stats = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, COUNT(*) as cnt "
                "FROM gptbridge_rag.outbox_event GROUP BY state"
            )
            for row in cur.fetchall():
                stats[row["state"]] = row["cnt"]
        return stats

class OutboxWorker:
    """Background worker that consumes outbox and applies to Qdrant."""

    def __init__(
        self,
        outbox_repo: OutboxRepository,
        qdrant_runtime: Any,  # QdrantCanonicalRuntime
        batch_size: int = 50,
        max_attempts: int = 5,
    ) -> None:
        self.outbox = outbox_repo
        self.qdrant = qdrant_runtime
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self._running = False

    async def process_batch(self) -> dict[str, int]:
        """Process one batch of pending events."""
        results = {"processed": 0, "succeeded": 0, "failed": 0, "dead_letter": 0}

        events = self.outbox.fetch_pending(self.batch_size)
        for event in events:
            results["processed"] += 1

            # Mark processing
            if not self.outbox.mark_processing(event.event_id):
                continue

            try:
                success = await self._apply_event(event)
                if success:
                    self.outbox.mark_done(event.event_id)
                    results["succeeded"] += 1
                else:
                    self.outbox.mark_failed(event.event_id, "apply_event returned False", self.max_attempts)
                    results["failed"] += 1
            except Exception as exc:
                self.outbox.mark_failed(event.event_id, f"{type(exc).__name__}: {exc}", self.max_attempts)
                results["failed"] += 1

        return results

    async def _apply_event(self, event: OutboxEvent) -> bool:
        """Apply single outbox event to Qdrant."""
        from qdrant_client.http.models import PointStruct
        from .rag_qdrant import sanitize_payload

        if event.operation == OutboxOperation.UPSERT:
            if not event.payload:
                return False
            point = PointStruct(
                id=event.payload.get("id", str(uuid.uuid4())),
                vector=event.payload["vector"],
                payload=sanitize_payload(event.payload["payload"]),
            )
            success = await self.qdrant.upsert_points([point], generation_id=event.generation_id)
            if success:
                self._record_point_lineage(event, point)
            return success

        elif event.operation == OutboxOperation.DELETE:
            return await self.qdrant.delete_resource(
                module_id=event.module_id,
                resource_id=event.resource_id,
                generation_id=event.generation_id,
            )

        elif event.operation == OutboxOperation.UPDATE_METADATA:
            # Metadata-only update: payload contains updated fields
            # This requires read-modify-write on Qdrant point
            # For simplicity, treat as UPSERT with full payload
            if not event.payload:
                return False
            point = PointStruct(
                id=event.payload.get("id", str(uuid.uuid4())),
                vector=event.payload["vector"],
                payload=sanitize_payload(event.payload["payload"]),
            )
            success = await self.qdrant.upsert_points([point], generation_id=event.generation_id)
            if success:
                self._record_point_lineage(event, point)
            return success

        _logger.warning("OutboxWorker: unknown operation %s", event.operation)
        return False

    def _record_point_lineage(self, event: OutboxEvent, point: Any) -> None:
        """Record an applied Qdrant point + embedded_from/indexed_from arcs.

        Best-effort: a lineage failure is only logged and never fails the
        outbox event.  A stable run_id (the outbox event id) keeps the graph
        idempotent across retries and reconciles.
        """
        if not LINEAGE_AVAILABLE:
            return
        try:
            payload = event.payload or {}
            meta = payload.get("payload") or {}
            chunk_id = str(meta.get("chunk_id") or point.id)
            module_id = event.module_id or meta.get("module_id")
            resource_id = event.resource_id or meta.get("resource_id")
            if not module_id or not resource_id:
                return
            with psycopg.connect(self.outbox.dsn) as conn:
                record_qdrant_point(
                    conn,
                    point_id=str(point.id),
                    chunk_id=chunk_id,
                    module_id=str(module_id),
                    resource_id=str(resource_id),
                    generation_id=event.generation_id,
                    embedding_model=meta.get("embedding_model"),
                    embedding_dimension=meta.get("embedding_dimension"),
                    run_id=str(event.event_id),
                    source_version=payload.get("source_version"),
                    content_hash=event.content_hash,
                    metadata={"operation": event.operation.value},
                )
        except Exception as exc:
            _logger.warning("OutboxWorker point lineage not recorded (non-fatal): %s", exc)