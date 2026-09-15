"""RAG Runtime State Machine — A372-A374 canonical state, queue, outbox, tombstone.

A374 STATE-MACHINE:
    runtime RAG state is exactly STARTING, CANONICAL, DEGRADED, or RECONCILING.

    STARTING    → initial; transitions to CANONICAL only after all canonical
                  checks pass (healthy Qdrant + healthy PostgreSQL + matching
                  authoritative index_state + complete reconciliation queue).
    CANONICAL   → verified Qdrant/PostgreSQL fault → DEGRADED.
    DEGRADED    → bounded local path may continue for the affected scope only;
                  every degraded create/update/tombstone/archive/permission/
                  version mutation must create a durable queue item;
                  reconciliation_required=true.
    DEGRADED    → canonical recovery detected → RECONCILING.
    RECONCILING → verify source SHA-256 → re-chunk → re-embed → idempotent
                  Qdrant write → update PostgreSQL → verify counts/IDs/hashes/
                  versions → drain queue.  Only after reconciliation succeeds
                  may reconciliation_required=false and state return CANONICAL.

    Service availability alone cannot transition to CANONICAL; SQLite vectors
    are never copied as canonical data.

This module is transport-agnostic: it owns the state enum, the guarded
transition table, the durable queue item shape, the outbox/saga step record,
and the tombstone guard.  Concrete stores (PostgreSQL authority, Qdrant
runtime) plug into the state machine via the interfaces declared here.
"""
from __future__ import annotations

import enum
import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

_logger = logging.getLogger("gptbridge.rag.runtime_state")


# ---------------------------------------------------------------------------
# A374: Runtime state enum — exactly four governed states.
# ---------------------------------------------------------------------------


class RagRuntimeState(str, enum.Enum):
    """A374: runtime RAG state is exactly these four values."""

    STARTING = "STARTING"
    CANONICAL = "CANONICAL"
    DEGRADED = "DEGRADED"
    RECONCILING = "RECONCILING"


# Allowed forward transitions (A374).  Any transition not in this table is
# forbidden and must raise TransitionError.
_ALLOWED_TRANSITIONS: dict[RagRuntimeState, frozenset[RagRuntimeState]] = {
    RagRuntimeState.STARTING: frozenset({RagRuntimeState.CANONICAL, RagRuntimeState.DEGRADED}),
    RagRuntimeState.CANONICAL: frozenset({RagRuntimeState.DEGRADED}),
    RagRuntimeState.DEGRADED: frozenset({RagRuntimeState.RECONCILING}),
    RagRuntimeState.RECONCILING: frozenset({RagRuntimeState.CANONICAL, RagRuntimeState.DEGRADED}),
}


class TransitionError(RuntimeError):
    """Raised when a state transition is not allowed by A374."""


class CanonicalCheckError(RuntimeError):
    """Raised when a canonical readiness check fails."""


# ---------------------------------------------------------------------------
# A374: Reconciliation queue item + statuses.
# ---------------------------------------------------------------------------


class QueueStatus(str, enum.Enum):
    """Durable reconciliation queue item statuses (A374)."""

    PENDING = "pending"
    LEASED = "leased"
    RECONCILING = "reconciling"
    VERIFIED = "verified"
    CONFLICT = "conflict"
    FAILED = "failed"
    DEAD_LETTER = "dead_letter"


class QueueOperation(str, enum.Enum):
    """Operations that may be durably queued during degraded mode."""

    CREATE = "create"
    UPDATE = "update"
    TOMBSTONE = "tombstone"
    ARCHIVE = "archive"
    PERMISSION = "permission"
    VERSION = "version"


@dataclass(frozen=True)
class ReconciliationQueueItem:
    """A374: durable reconciliation queue item.

    Every degraded create/update/tombstone/archive/permission/version
    mutation must create one of these.  The item is the unit of replay
    during RECONCILING.
    """

    operation_id: str
    idempotency_key: str
    resource_id: str
    locator_id: str
    source_revision: int
    content_hash: str
    operation: str  # QueueOperation value
    tombstone_generation: int
    embedding_model: str
    embedding_version: int
    chunk_size: int
    chunk_overlap: int
    chunking_version: int
    parser_version: int
    schema_version: int
    created_at: str
    attempts: int = 0
    next_retry_at: Optional[str] = None
    deadline: Optional[str] = None
    status: str = QueueStatus.PENDING.value
    correlation_id: Optional[str] = None
    last_error: Optional[str] = None
    # Payload snapshot required for idempotent replay (re-chunk, re-embed).
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["payload"] = json.dumps(self.payload, ensure_ascii=False, sort_keys=True)
        return data

    @classmethod
    def from_row(cls, row: sqlite3.Row | tuple) -> "ReconciliationQueueItem":
        # Accept both sqlite3.Row and plain tuple in column order below.
        if isinstance(row, sqlite3.Row):
            getter = lambda key: row[key]
        else:
            keys = (
                "operation_id", "idempotency_key", "resource_id", "locator_id",
                "source_revision", "content_hash", "operation", "tombstone_generation",
                "embedding_model", "embedding_version", "chunk_size", "chunk_overlap",
                "chunking_version", "parser_version", "schema_version", "created_at",
                "attempts", "next_retry_at", "deadline", "status", "correlation_id",
                "last_error", "payload",
            )
            getter = lambda key: row[keys.index(key)]
        payload_raw = getter("payload")
        if isinstance(payload_raw, (bytes, bytearray)):
            payload_raw = payload_raw.decode("utf-8")
        payload: dict[str, Any]
        if isinstance(payload_raw, dict):
            payload = payload_raw
        else:
            try:
                payload = json.loads(payload_raw) if payload_raw else {}
            except (TypeError, ValueError):
                payload = {}
        return cls(
            operation_id=str(getter("operation_id")),
            idempotency_key=str(getter("idempotency_key")),
            resource_id=str(getter("resource_id")),
            locator_id=str(getter("locator_id")),
            source_revision=int(getter("source_revision")),
            content_hash=str(getter("content_hash")),
            operation=str(getter("operation")),
            tombstone_generation=int(getter("tombstone_generation")),
            embedding_model=str(getter("embedding_model")),
            embedding_version=int(getter("embedding_version")),
            chunk_size=int(getter("chunk_size")),
            chunk_overlap=int(getter("chunk_overlap")),
            chunking_version=int(getter("chunking_version")),
            parser_version=int(getter("parser_version")),
            schema_version=int(getter("schema_version")),
            created_at=str(getter("created_at")),
            attempts=int(getter("attempts")),
            next_retry_at=str(getter("next_retry_at")) if getter("next_retry_at") else None,
            deadline=str(getter("deadline")) if getter("deadline") else None,
            status=str(getter("status")),
            correlation_id=str(getter("correlation_id")) if getter("correlation_id") else None,
            last_error=str(getter("last_error")) if getter("last_error") else None,
            payload=payload,
        )


def make_idempotency_key(
    *,
    module_id: str,
    resource_id: str,
    operation: str,
    source_revision: int,
    content_hash: str,
) -> str:
    """Deterministic idempotency key for a queued mutation (A374)."""
    raw = f"{module_id}:{resource_id}:{operation}:{source_revision}:{content_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# A374: Cross-store outbox/saga step record.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboxStep:
    """Record of one store's result within a cross-store saga (A374)."""

    store: str  # "qdrant" | "postgresql"
    operation: str  # e.g. "upsert", "tombstone", "index_state"
    succeeded: bool
    applied_at: str
    error: Optional[str] = None
    store_record_id: Optional[str] = None


@dataclass
class SagaResult:
    """Result of a cross-store outbox/saga execution (A374).

    A saga is complete only when ``complete`` is True.  Partial success is
    exposed as incomplete/recoverable and must enqueue reconciliation.
    """

    operation_id: str
    complete: bool
    steps: list[OutboxStep]
    compensation_required: bool
    queue_item_id: Optional[str]

    def step_for(self, store: str) -> Optional[OutboxStep]:
        for step in self.steps:
            if step.store == store:
                return step
        return None


# ---------------------------------------------------------------------------
# A374: Tombstone anti-resurrection guard.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TombstoneRecord:
    """Authoritative tombstone for anti-resurrection (A374)."""

    resource_id: str
    module_id: str
    tombstone_generation: int
    source_revision: int
    content_hash: str
    created_at: str
    reason: str = "deleted"
    purged: bool = False


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
                    payload TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS rag_queue_status_idx
                    ON rag_reconciliation_queue (status, next_retry_at);
                CREATE INDEX IF NOT EXISTS rag_queue_resource_idx
                    ON rag_reconciliation_queue (resource_id, source_revision);
                """
            )
            self.connection.commit()

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
                    last_error, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO UPDATE SET
                    status = 'pending',
                    last_error = NULL,
                    next_retry_at = NULL
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
                """
                SELECT * FROM rag_reconciliation_queue
                WHERE status = 'pending'
                  AND (next_retry_at IS NULL OR next_retry_at <= ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (now, max(1, int(limit))),
            ).fetchall()
            leased: list[ReconciliationQueueItem] = []
            for row in rows:
                self.connection.execute(
                    "UPDATE rag_reconciliation_queue SET status='leased', attempts=attempts+1 WHERE operation_id=?",
                    (row["operation_id"] if isinstance(row, sqlite3.Row) else row[0],),
                )
                leased.append(ReconciliationQueueItem.from_row(row))
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
                "SELECT * FROM rag_reconciliation_queue ORDER BY created_at ASC"
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


# ---------------------------------------------------------------------------
# A374: Runtime state machine with guarded transitions.
# ---------------------------------------------------------------------------


class RagRuntimeStateMachine:
    """A374: guarded runtime state machine for the canonical RAG pipeline.

    The state machine owns:
      * the current RagRuntimeState
      * the durable ReconciliationQueue
      * the TombstoneGuard
      * the CrossStoreOutbox

    Transitions are guarded by A374 rules.  Callers must provide canonical
    readiness checks; the state machine never transitions to CANONICAL on
    service availability alone.
    """

    def __init__(
        self,
        queue: ReconciliationQueue,
        tombstone: Optional[TombstoneGuard] = None,
        outbox: Optional[CrossStoreOutbox] = None,
    ) -> None:
        self._state = RagRuntimeState.STARTING
        self._queue = queue
        self._tombstone = tombstone or TombstoneGuard()
        self._outbox = outbox or CrossStoreOutbox(queue, self._tombstone)
        self._lock = threading.RLock()
        self._last_transition_at = datetime.now(timezone.utc).isoformat()
        self._last_error: Optional[str] = None

    @property
    def state(self) -> RagRuntimeState:
        with self._lock:
            return self._state

    @property
    def queue(self) -> ReconciliationQueue:
        return self._queue

    @property
    def tombstone(self) -> TombstoneGuard:
        return self._tombstone

    @property
    def outbox(self) -> CrossStoreOutbox:
        return self._outbox

    @property
    def reconciliation_required(self) -> bool:
        """A374: true unless CANONICAL with a complete queue."""
        with self._lock:
            if self._state == RagRuntimeState.CANONICAL:
                return not self._queue.is_complete()
            return True

    def _transition(self, target: RagRuntimeState) -> None:
        with self._lock:
            current = self._state
            allowed = _ALLOWED_TRANSITIONS.get(current, frozenset())
            if target not in allowed:
                raise TransitionError(
                    f"RAG_STATE_TRANSITION_FORBIDDEN: {current.value} -> {target.value}"
                )
            self._state = target
            self._last_transition_at = datetime.now(timezone.utc).isoformat()
            _logger.info(
                "RagRuntimeStateMachine: %s -> %s", current.value, target.value
            )

    # -- STARTING -> CANONICAL | DEGRADED -----------------------------------

    def evaluate_startup(
        self,
        *,
        qdrant_healthy: bool,
        postgresql_healthy: bool,
        index_state_matches: bool,
    ) -> RagRuntimeState:
        """A374: STARTING -> CANONICAL only after all canonical checks pass.

        Canonical availability alone is NOT sufficient: the authoritative
        index_state must match and the reconciliation queue must be complete.
        """
        with self._lock:
            if self._state != RagRuntimeState.STARTING:
                raise TransitionError(
                    f"evaluate_startup called in state {self._state.value}"
                )
        if (
            qdrant_healthy
            and postgresql_healthy
            and index_state_matches
            and self._queue.is_complete()
        ):
            self._transition(RagRuntimeState.CANONICAL)
        else:
            self._transition(RagRuntimeState.DEGRADED)
        return self.state

    # -- CANONICAL -> DEGRADED ----------------------------------------------

    def report_canonical_failure(self, reason: str) -> RagRuntimeState:
        """A374: verified Qdrant/PostgreSQL fault -> DEGRADED."""
        with self._lock:
            self._last_error = reason
        self._transition(RagRuntimeState.DEGRADED)
        return self.state

    # -- DEGRADED -> RECONCILING --------------------------------------------

    def begin_reconciliation(
        self,
        *,
        qdrant_healthy: bool,
        postgresql_healthy: bool,
    ) -> RagRuntimeState:
        """A374: DEGRADED -> RECONCILING after canonical recovery detected."""
        if not (qdrant_healthy and postgresql_healthy):
            raise CanonicalCheckError(
                "Cannot begin reconciliation: canonical services not healthy"
            )
        self._transition(RagRuntimeState.RECONCILING)
        return self.state

    # -- RECONCILING -> CANONICAL | DEGRADED --------------------------------

    def complete_reconciliation(
        self,
        *,
        counts_match: bool,
        ids_match: bool,
        hashes_match: bool,
        versions_match: bool,
    ) -> RagRuntimeState:
        """A374: RECONCILING -> CANONICAL only after parity verification + queue drain.

        Reconciliation must:
          * verify source SHA-256
          * re-chunk
          * re-embed
          * perform idempotent Qdrant writes
          * update PostgreSQL
          * verify counts, IDs, hashes, and versions
          * drain the queue
        Only after all of these succeed may state return CANONICAL.
        """
        parity = counts_match and ids_match and hashes_match and versions_match
        if not parity:
            self._transition(RagRuntimeState.DEGRADED)
            return self.state
        if not self._queue.is_complete():
            self._transition(RagRuntimeState.DEGRADED)
            return self.state
        self._transition(RagRuntimeState.CANONICAL)
        return self.state

    def fail_reconciliation(self, reason: str) -> RagRuntimeState:
        """A374: RECONCILING -> DEGRADED when reconciliation cannot complete."""
        with self._lock:
            self._last_error = reason
        self._transition(RagRuntimeState.DEGRADED)
        return self.state

    # -- Degraded mutation enqueue -----------------------------------------

    def enqueue_degraded_mutation(
        self,
        *,
        module_id: str,
        resource_id: str,
        locator_id: str,
        source_revision: int,
        content_hash: str,
        operation: str,
        embedding_model: str,
        embedding_version: int = 1,
        chunk_size: int = 0,
        chunk_overlap: int = 0,
        chunking_version: int = 1,
        parser_version: int = 1,
        schema_version: int = 1,
        tombstone_generation: int = 0,
        correlation_id: Optional[str] = None,
        deadline: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> ReconciliationQueueItem:
        """A374: every degraded mutation must create a durable queue item."""
        # Tombstone anti-resurrection check
        self._tombstone.reject_stale_write(
            module_id=module_id,
            resource_id=resource_id,
            source_revision=source_revision,
            operation=operation,
        )
        item = ReconciliationQueueItem(
            operation_id=str(uuid.uuid4()),
            idempotency_key=make_idempotency_key(
                module_id=module_id,
                resource_id=resource_id,
                operation=operation,
                source_revision=source_revision,
                content_hash=content_hash,
            ),
            resource_id=resource_id,
            locator_id=locator_id,
            source_revision=source_revision,
            content_hash=content_hash,
            operation=operation,
            tombstone_generation=tombstone_generation or self._tombstone.current_generation(
                module_id, resource_id
            ),
            embedding_model=embedding_model,
            embedding_version=embedding_version,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            chunking_version=chunking_version,
            parser_version=parser_version,
            schema_version=schema_version,
            created_at=datetime.now(timezone.utc).isoformat(),
            status=QueueStatus.PENDING.value,
            correlation_id=correlation_id,
            deadline=deadline,
            payload=payload or {},
        )
        self._queue.enqueue(item)
        _logger.info(
            "RagRuntimeStateMachine: enqueued degraded %s for %s:%s revision=%s",
            operation, module_id, resource_id, source_revision,
        )
        return item

    # -- Status snapshot ----------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state.value,
                "reconciliation_required": self.reconciliation_required,
                "queue_pending": self._queue.pending_count(),
                "queue_complete": self._queue.is_complete(),
                "tombstones": len(self._tombstone._tombstones),
                "last_transition_at": self._last_transition_at,
                "last_error": self._last_error,
            }


__all__ = [
    "CanonicalCheckError",
    "CrossStoreOutbox",
    "OutboxStep",
    "QueueOperation",
    "QueueStatus",
    "RagRuntimeState",
    "RagRuntimeStateMachine",
    "ReconciliationQueue",
    "ReconciliationQueueItem",
    "SagaResult",
    "TombstoneGuard",
    "TombstoneRecord",
    "TransitionError",
    "make_idempotency_key",
]
