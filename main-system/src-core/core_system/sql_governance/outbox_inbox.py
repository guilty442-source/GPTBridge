"""Outbox/Inbox Pattern — A506 正式化。

TRANSACTIONAL_OUTBOX: canonical write path 的一部分
INBOX_DEDUP: consumer 端冪等

Outbox → Transport → Inbox → Executor
完整 exactly-once effect 模型。
"""

from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import psycopg
from psycopg.rows import dict_row

from .sql_concurrency import OutboxContract, InboxDedup, OutboxState, OutboxOperation

_logger = logging.getLogger("gptbridge.sql.outbox_inbox")


class InboxError(Exception):
    """Inbox operation error."""
    pass


class DuplicateMessageError(InboxError):
    """Duplicate message detected."""
    pass


# ============================================================================
# Transactional Outbox (Canonical Write Path Component)
# ============================================================================

class TransactionalOutbox:
    """Transactional Outbox - 寫入與 canonical data 同一 transaction (A506).

    正確流程:
    BEGIN
      canonical data write
      + outbox event write
    COMMIT

    之後 transport worker:
      read outbox
      → publish
      → delivery receipt
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                autocommit=False,
            )
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def create_event(
        self,
        cur: psycopg.Cursor,
        request_id: str,
        operation: OutboxOperation,
        module_id: str,
        resource_id: str,
        source_version: int,
        content_hash: str,
        generation_id: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> str:
        """Create outbox event WITHIN the canonical transaction.

        Must be called within the same transaction that writes canonical data.
        """
        import uuid
        event_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        cur.execute(
            """
            INSERT INTO gptbridge_rag.outbox_event
            (event_id, request_id, operation, module_id, resource_id,
             source_version, content_hash, generation_id, payload, state,
             attempt_count, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event_id,
                request_id,
                operation.value,
                module_id,
                resource_id,
                source_version,
                content_hash,
                generation_id,
                json.dumps(payload) if payload else None,
                OutboxState.PENDING.value,
                0,
                now, now,
            ),
        )
        return event_id

    def fetch_pending(self, limit: int = 100) -> list[OutboxContract]:
        """Fetch PENDING events for processing (FOR UPDATE SKIP LOCKED)."""
        conn = self._get_conn()
        events = []
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT event_id, request_id, operation, module_id, resource_id,
                       source_version, content_hash, generation_id, payload,
                       state, attempt_count, next_retry_at, last_error,
                       created_at, updated_at, completed_at
                FROM gptbridge_rag.outbox_event
                WHERE state = 'PENDING'
                ORDER BY created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            for row in cur.fetchall():
                events.append(OutboxContract(
                    event_id=str(row["event_id"]),
                    request_id=row["request_id"],
                    operation=OutboxOperation(row["operation"]),
                    module_id=row["module_id"],
                    resource_id=row["resource_id"],
                    source_version=row["source_version"],
                    content_hash=row["content_hash"],
                    generation_id=row["generation_id"],
                    payload=row["payload"],
                    state=OutboxState(row["state"]),
                    attempt_count=row["attempt_count"],
                    next_retry_at=row["next_retry_at"],
                    last_error=row["last_error"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    completed_at=row["completed_at"],
                ))
        return events

    def mark_processing(self, event_id: str) -> bool:
        """Mark event as PROCESSING."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.outbox_event
                SET state = 'PROCESSING', updated_at = %s
                WHERE event_id = %s AND state = 'PENDING'
                """,
                (datetime.now(timezone.utc).isoformat(), event_id),
            )
            return cur.rowcount > 0

    def mark_succeeded(self, event_id: str) -> bool:
        """Mark event as SUCCEEDED."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE gptbridge_rag.outbox_event
                SET state = 'SUCCEEDED', updated_at = %s, completed_at = %s
                WHERE event_id = %s
                """,
                (datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), event_id),
            )
            return cur.rowcount > 0

    def mark_retry(self, event_id: str, error: str, max_attempts: int = 5) -> bool:
        """Mark event as RETRY or DEAD_LETTER."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT attempt_count FROM gptbridge_rag.outbox_event WHERE event_id = %s",
                (event_id,),
            )
            row = cur.fetchone()
            if not row:
                return False
            attempts = row["attempt_count"] + 1
            new_state = OutboxState.DEAD_LETTER.value if attempts >= max_attempts else OutboxState.RETRY.value

            next_retry = None
            if new_state == OutboxState.RETRY.value:
                # Exponential backoff
                delay = min(2 ** attempts, 300)  # Max 5 minutes
                next_retry = datetime.now(timezone.utc).isoformat()

            cur.execute(
                """
                UPDATE gptbridge_rag.outbox_event
                SET state = %s, attempt_count = %s, last_error = %s,
                    next_retry_at = %s, updated_at = %s
                WHERE event_id = %s
                """,
                (new_state, attempts, error, next_retry, datetime.now(timezone.utc).isoformat(), event_id),
            )
            return cur.rowcount > 0

    def get_stats(self) -> dict[str, int]:
        """Get outbox statistics."""
        conn = self._get_conn()
        stats = {}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state, COUNT(*) as cnt FROM gptbridge_rag.outbox_event GROUP BY state"
            )
            for row in cur.fetchall():
                stats[row["state"]] = row["cnt"]
        return stats


# ============================================================================
# Inbox Deduplication (Exactly-Once Effect)
# ============================================================================

class InboxDedupProcessor:
    """Inbox deduplication for exactly-once effect (A506).

    Producer 端 retry 不會造成 consumer 重複執行。
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                autocommit=True,
            )
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    def check_and_mark(
        self,
        message_id: str,
        idempotency_key: str,
        executor: Callable[[], Any],
    ) -> InboxDedup:
        """Check deduplication and execute if new.

        Returns InboxDedup with result.
        """
        conn = self._get_conn()
        now = datetime.now(timezone.utc).isoformat()

        try:
            with conn.cursor() as cur:
                # Try to insert new message
                cur.execute(
                    """
                    INSERT INTO gptbridge_rag.inbox_dedup
                    (message_id, idempotency_key, received_at, processed_at, receipt, duplicate)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        received_at = EXCLUDED.received_at
                    RETURNING message_id, idempotency_key, received_at, processed_at, receipt, duplicate
                    """,
                    (message_id, idempotency_key, now, None, None, False),
                )
                row = cur.fetchone()

                if row and not row["duplicate"]:
                    # New message - execute
                    try:
                        result = executor()
                        receipt = {"result": result, "executed_at": datetime.now(timezone.utc).isoformat()}

                        cur.execute(
                            """
                            UPDATE gptbridge_rag.inbox_dedup
                            SET processed_at = %s, receipt = %s, duplicate = false
                            WHERE idempotency_key = %s
                            """,
                            (datetime.now(timezone.utc).isoformat(), json.dumps(result), idempotency_key),
                        )
                        return InboxDedup(
                            message_id=message_id,
                            idempotency_key=idempotency_key,
                            received_at=row["received_at"],
                            processed_at=datetime.now(timezone.utc).isoformat(),
                            receipt=receipt,
                            duplicate=False,
                        )
                    except Exception as e:
                        # Mark as failed
                        cur.execute(
                            """
                            UPDATE gptbridge_rag.inbox_dedup
                            SET processed_at = %s, receipt = %s, duplicate = false
                            WHERE idempotency_key = %s
                            """,
                            (datetime.now(timezone.utc).isoformat(), json.dumps({"error": str(e)}), idempotency_key),
                        )
                        raise

                elif row and row["duplicate"]:
                    # Duplicate - return prior receipt
                    return InboxDedup(
                        message_id=message_id,
                        idempotency_key=idempotency_key,
                        received_at=row["received_at"],
                        processed_at=row["processed_at"],
                        receipt=row["receipt"],
                        duplicate=True,
                    )

                # Fallback: fetch existing
                cur.execute(
                    "SELECT message_id, idempotency_key, received_at, "
                    "processed_at, receipt, duplicate "
                    "FROM gptbridge_rag.inbox_dedup WHERE idempotency_key = %s",
                    (idempotency_key,),
                )
                existing = cur.fetchone()
                if existing:
                    return InboxDedup(
                        message_id=existing["message_id"],
                        idempotency_key=existing["idempotency_key"],
                        received_at=existing["received_at"],
                        processed_at=existing["processed_at"],
                        receipt=existing["receipt"],
                        duplicate=True,
                    )

                raise InboxError("Unexpected state in inbox deduplication")

        except psycopg.errors.UniqueViolation:
            # Race condition - fetch existing
            conn.rollback()
            return self.check_and_mark(message_id, idempotency_key, executor)

    def get_receipt(self, idempotency_key: str) -> Optional[dict]:
        """Get existing receipt for idempotency key."""
        conn = self._get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT message_id, idempotency_key, received_at, "
                "processed_at, receipt, duplicate "
                "FROM gptbridge_rag.inbox_dedup WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            row = cur.fetchone()
            if row:
                return dict(row)
        return None


# ============================================================================
# Outbox Worker (Background Processor)
# ============================================================================

class OutboxWorker:
    """Background worker that processes outbox events."""

    def __init__(
        self,
        outbox: TransactionalOutbox,
        publisher: Callable[[OutboxContract], bool],  # Returns True on success
        batch_size: int = 50,
        max_attempts: int = 5,
    ) -> None:
        self.outbox = outbox
        self.publisher = publisher
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self._running = False

    def process_batch(self) -> dict[str, int]:
        """Process one batch of pending events."""
        results = {"processed": 0, "succeeded": 0, "failed": 0, "dead_letter": 0}

        events = self.outbox.fetch_pending(self.batch_size)
        for event in events:
            results["processed"] += 1

            if not self.outbox.mark_processing(event.event_id):
                continue

            try:
                success = self.publisher(event)
                if success:
                    self.outbox.mark_succeeded(event.event_id)
                    results["succeeded"] += 1
                else:
                    self.outbox.mark_retry(event.event_id, "Publisher returned False", self.max_attempts)
                    results["failed"] += 1
            except Exception as e:
                self.outbox.mark_retry(event.event_id, f"{type(e).__name__}: {e}", self.max_attempts)
                results["failed"] += 1

        return results


# Table schemas
OUTBOX_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gptbridge_rag.outbox_event (
    event_id UUID PRIMARY KEY,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('UPSERT','DELETE','REINDEX','UPDATE_METADATA')),
    module_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    payload JSONB,
    state TEXT NOT NULL CHECK (state IN ('PENDING','PROCESSING','SUCCEEDED','RETRY','DEAD_LETTER')),
    attempt_count INTEGER DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_event_state_created
ON gptbridge_rag.outbox_event (state, created_at);

CREATE INDEX IF NOT EXISTS idx_outbox_event_generation
ON gptbridge_rag.outbox_event (generation_id, state);

CREATE INDEX IF NOT EXISTS idx_outbox_event_retry
ON gptbridge_rag.outbox_event (next_retry_at)
WHERE state = 'RETRY';

-- Updated at trigger
CREATE OR REPLACE FUNCTION gptbridge_rag.update_outbox_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_outbox_event_updated_at ON gptbridge_rag.outbox_event;
CREATE TRIGGER trg_outbox_event_updated_at
    BEFORE UPDATE ON gptbridge_rag.outbox_event
    FOR EACH ROW EXECUTE FUNCTION gptbridge_rag.update_outbox_updated_at();
"""

INBOX_DEDUP_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gptbridge_rag.inbox_dedup (
    message_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    received_at TIMESTAMPTZ NOT NULL,
    processed_at TIMESTAMPTZ,
    receipt JSONB,
    duplicate BOOLEAN DEFAULT FALSE
);

CREATE INDEX IF NOT EXISTS idx_inbox_dedup_received
ON gptbridge_rag.inbox_dedup (received_at);

CREATE INDEX IF NOT EXISTS idx_inbox_dedup_processed
ON gptbridge_rag.inbox_dedup (processed_at);
"""


__all__ = [
    "TransactionalOutbox",
    "OutboxContract",
    "OutboxState",
    "OutboxOperation",
    "OutboxWorker",
    "InboxDedupProcessor",
    "InboxDedup",
    "DuplicateMessageError",
    "OUTBOX_TABLE_SQL",
    "INBOX_DEDUP_TABLE_SQL",
]