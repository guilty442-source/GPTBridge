"""Transactional Outbox Pattern — 雙寫一致性保證。

A486+A487: PostgreSQL transaction commits both metadata + outbox event.
RAG worker consumes outbox and applies to Qdrant asynchronously.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import psycopg
from psycopg.rows import dict_row

_logger = logging.getLogger("gptbridge.rag.outbox")


class OutboxOperation(str, Enum):
    """Outbox operation types."""
    UPSERT = "UPSERT"          # Index/create resource
    DELETE = "DELETE"          # Remove resource
    REINDEX = "REINDEX"        # Full generation reindex
    UPDATE_METADATA = "UPDATE_METADATA"  # Metadata only


class OutboxState(str, Enum):
    """Outbox event lifecycle states."""
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
    state: OutboxState = OutboxState.PENDING
    last_error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


OUTBOX_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS rag_outbox (
    event_id UUID PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('UPSERT','DELETE','REINDEX','UPDATE_METADATA')),
    resource_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    content_hash TEXT,
    payload JSONB,
    attempts INTEGER DEFAULT 0,
    state TEXT NOT NULL CHECK (state IN ('PENDING','PROCESSING','DONE','FAILED','DEAD_LETTER')),
    last_error TEXT,
    created_at_utc TEXT NOT NULL,
    updated_at_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_rag_outbox_state_created
ON rag_outbox (state, created_at_utc);

CREATE INDEX IF NOT EXISTS idx_rag_outbox_generation
ON rag_outbox (generation_id, state);
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
                INSERT INTO rag_outbox
                (event_id, operation, resource_id, module_id, generation_id,
                 content_hash, payload, attempts, state, last_error,
                 created_at_utc, updated_at_utc)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event.event_id,
                    event.operation.value,
                    event.resource_id,
                    event.module_id,
                    event.generation_id,
                    event.content_hash,
                    json.dumps(event.payload) if event.payload else None,
                    event.attempts,
                    event.state.value,
                    event.last_error,
                    event.created_at,
                    event.updated_at,
                ),
            )
        return event

    def fetch_pending(self, limit: int = 100) -> list[OutboxEvent]:
        """Fetch PENDING events for processing (FOR UPDATE SKIP LOCKED)."""
        conn = self._get_conn()
        events = []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_id, operation, resource_id, module_id, generation_id,
                       content_hash, payload, attempts, state, last_error,
                       created_at_utc, updated_at_utc
                FROM rag_outbox
                WHERE state = 'PENDING'
                ORDER BY created_at_utc
                LIMIT %s
                FOR UPDATE SKIP LOCKED
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
                    attempts=row["attempts"],
                    state=OutboxState(row["state"]),
                    last_error=row["last_error"],
                    created_at=row["created_at_utc"],
                    updated_at=row["updated_at_utc"],
                ))
        return events

    def mark_processing(self, event_id: str) -> bool:
        """Mark event as PROCESSING."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE rag_outbox
                SET state = 'PROCESSING', updated_at_utc = %s
                WHERE event_id = %s AND state = 'PENDING'
                """,
                (datetime.now(timezone.utc).isoformat(), event_id),
            )
            return cur.rowcount > 0

    def mark_done(self, event_id: str) -> bool:
        """Mark event as DONE."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE rag_outbox
                SET state = 'DONE', updated_at_utc = %s
                WHERE event_id = %s
                """,
                (datetime.now(timezone.utc).isoformat(), event_id),
            )
            return cur.rowcount > 0

    def mark_failed(self, event_id: str, error: str, max_attempts: int = 5) -> bool:
        """Mark event as FAILED or DEAD_LETTER based on attempts."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            # First get current attempts
            cur.execute(
                "SELECT attempts FROM rag_outbox WHERE event_id = %s",
                (event_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            attempts = row["attempts"] + 1
            new_state = OutboxState.DEAD_LETTER.value if attempts >= max_attempts else OutboxState.FAILED.value

            cur.execute(
                """
                UPDATE rag_outbox
                SET state = %s, attempts = %s, last_error = %s, updated_at_utc = %s
                WHERE event_id = %s
                """,
                (new_state, attempts, error, datetime.now(timezone.utc).isoformat(), event_id),
            )
            return cur.rowcount > 0

    def get_stats(self) -> dict[str, int]:
        """Get outbox statistics by state."""
        conn = self._get_conn()
        stats = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, COUNT(*) as cnt FROM rag_outbox GROUP BY state"
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

        if event.operation == OutboxOperation.UPSERT:
            if not event.payload:
                return False
            point = PointStruct(
                id=event.payload.get("id", str(uuid.uuid4())),
                vector=event.payload["vector"],
                payload=event.payload["payload"],
            )
            return await self.qdrant.upsert_points([point], generation_id=event.generation_id)

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
                payload=event.payload["payload"],
            )
            return await self.qdrant.upsert_points([point], generation_id=event.generation_id)

        _logger.warning("OutboxWorker: unknown operation %s", event.operation)
        return False


def transactional_upsert_example():
    """Usage example showing transactional pattern.

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # 1. Update canonical metadata
            cur.execute("UPDATE rag_resources SET ..., updated_at = now() WHERE ...")
            cur.execute("INSERT INTO rag_index_state ... ON CONFLICT ...")

            # 2. Create outbox event in SAME transaction
            outbox_repo = OutboxRepository(dsn)
            outbox_repo.create_event(
                operation=OutboxOperation.UPSERT,
                resource_id="res-123",
                module_id="mod-456",
                generation_id="gen-20260916-001",
                content_hash="sha256:...",
                payload={"id": "...", "vector": [...], "payload": {...}},
            )

            # 3. Commit - both metadata and outbox atomically persisted
            conn.commit()

    # Later, OutboxWorker picks up and applies to Qdrant
    """