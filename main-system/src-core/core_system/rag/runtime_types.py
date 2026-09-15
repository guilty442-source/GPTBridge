"""RAG runtime types — A372-A374 state enum, records and idempotency keys.

Transport-agnostic value objects shared by the queue stores and the
runtime state machine.  See ``runtime_state.py`` for the governed
transition semantics (STARTING / CANONICAL / DEGRADED / RECONCILING).
The status surface additionally reports the derived outcome
RECONCILIATION_FAILED when a RECONCILING attempt failed and the service
stays bounded at DEGRADED; it is not a fifth machine state (A374).
"""
from __future__ import annotations

import enum
import hashlib
import json
import sqlite3
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

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
    # When the degraded store indexed the mutation, and when the canonical
    # stores confirmed the reconciled write (pending_rag_mutation fields).
    degraded_indexed_at: Optional[str] = None
    canonical_synced_at: Optional[str] = None
    # Payload snapshot required for idempotent replay (re-chunk, re-embed).
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def retry_count(self) -> int:
        """Retry counter (internal column name is ``attempts``)."""
        return self.attempts

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["payload"] = json.dumps(self.payload, ensure_ascii=False, sort_keys=True)
        return data

    @classmethod
    def from_row(cls, row: sqlite3.Row | tuple) -> "ReconciliationQueueItem":
        # Accept both sqlite3.Row and plain tuple in column order below.
        if isinstance(row, sqlite3.Row):
            available = set(row.keys())
            getter = lambda key: row[key] if key in available else None
        else:
            keys = (
                "operation_id", "idempotency_key", "resource_id", "locator_id",
                "source_revision", "content_hash", "operation", "tombstone_generation",
                "embedding_model", "embedding_version", "chunk_size", "chunk_overlap",
                "chunking_version", "parser_version", "schema_version", "created_at",
                "attempts", "next_retry_at", "deadline", "status", "correlation_id",
                "last_error", "payload", "degraded_indexed_at", "canonical_synced_at",
            )
            getter = (
                lambda key: row[keys.index(key)]
                if key in keys and keys.index(key) < len(row)
                else None
            )
        payload = _row_payload(getter("payload"))
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
            **_optional_recon_fields(getter),
        )


def _row_payload(raw: Any) -> dict[str, Any]:
    """Decode the queue payload column (JSON text / dict / bytes)."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}


def _optional_recon_fields(getter: Any) -> dict[str, Any]:
    """Optional pending_rag_mutation columns absent from older rows."""
    return {
        field: (str(getter(field)) if getter(field) else None)
        for field in ("degraded_indexed_at", "canonical_synced_at")
    }


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
