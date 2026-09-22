"""RAG Runtime State Machine — A372-A374 canonical state, queue, outbox, tombstone.

A374 STATE-MACHINE:
    runtime RAG state is exactly STARTING, CANONICAL, DEGRADED, or RECONCILING.

    STARTING    -> initial; transitions to CANONICAL only after all canonical
                  checks pass (healthy Qdrant + healthy PostgreSQL + matching
                  authoritative index_state + complete reconciliation queue).
    CANONICAL   -> verified Qdrant/PostgreSQL fault -> DEGRADED.
    DEGRADED    -> bounded local path may continue for the affected scope only;
                  every degraded create/update/tombstone/archive/permission/
                  version mutation must create a durable queue item;
                  reconciliation_required=true.
    DEGRADED    -> canonical recovery detected -> RECONCILING.
    RECONCILING -> verify source SHA-256 -> re-chunk -> re-embed -> idempotent
                  Qdrant write -> update PostgreSQL -> verify counts/IDs/hashes/
                  versions -> drain queue.  Only after reconciliation succeeds
                  may reconciliation_required=false and state return CANONICAL.

    Service availability alone cannot transition to CANONICAL; SQLite vectors
    are never copied as canonical data.

Types live in ``runtime_types.py``; durable stores in ``runtime_queue.py``.
Both are re-exported here so ``from .runtime_state import X`` keeps working.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .runtime_types import (
    _ALLOWED_TRANSITIONS,
    CanonicalCheckError,
    OutboxStep,
    QueueOperation,
    QueueStatus,
    RagRuntimeState,
    ReconciliationQueueItem,
    SagaResult,
    TombstoneRecord,
    TransitionError,
    make_idempotency_key,
)
from .runtime_queue import (
    CrossStoreOutbox,
    ReconciliationQueue,
    TombstoneGuard,
)

_logger = logging.getLogger("gptbridge.rag.runtime_state")


def _queue_item(
    *,
    operation_id: str,
    tombstone_generation: int,
    correlation_id: Optional[str],
    deadline: Optional[str],
    payload: Optional[dict[str, Any]],
    identity: dict[str, Any],
    versions: dict[str, Any],
) -> ReconciliationQueueItem:
    """Build a durable reconciliation queue item for a degraded mutation."""
    now = datetime.now(timezone.utc).isoformat()
    return ReconciliationQueueItem(
        operation_id=operation_id,
        idempotency_key=make_idempotency_key(
            module_id=identity["module_id"],
            resource_id=identity["resource_id"],
            operation=identity["operation"],
            source_revision=identity["source_revision"],
            content_hash=identity["content_hash"],
        ),
        tombstone_generation=tombstone_generation,
        created_at=now,
        degraded_indexed_at=now,
        status=QueueStatus.PENDING.value,
        correlation_id=correlation_id,
        deadline=deadline,
        payload=payload or {},
        **{k: v for k, v in identity.items() if k != "module_id"},
        **versions,
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
        self._reconciliation_failed = False

    @property
    def state(self) -> RagRuntimeState:
        with self._lock:
            return self._state

    @property
    def reconciliation_failed(self) -> bool:
        """True after a RECONCILING attempt failed; service stays bounded DEGRADED."""
        with self._lock:
            return self._reconciliation_failed

    @property
    def effective_state(self) -> str:
        """Status-surface state: RECONCILIATION_FAILED is a derived outcome of
        DEGRADED after a failed reconciliation (A374 keeps exactly four
        machine states; the failure is reported, not a fifth state)."""
        with self._lock:
            if self._state == RagRuntimeState.DEGRADED and self._reconciliation_failed:
                return "RECONCILIATION_FAILED"
            return self._state.value

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
            with self._lock:
                self._reconciliation_failed = False
            self._transition(RagRuntimeState.CANONICAL)
        else:
            self._transition(RagRuntimeState.DEGRADED)
        return self.state

    # -- CANONICAL -> DEGRADED ----------------------------------------------

    def report_canonical_failure(self, reason: str) -> RagRuntimeState:
        """A374: verified Qdrant/PostgreSQL fault -> DEGRADED (idempotent)."""
        with self._lock:
            self._last_error = reason
            current = self._state
        if current != RagRuntimeState.DEGRADED:
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
        with self._lock:
            self._reconciliation_failed = False
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
        if not parity or not self._queue.is_complete():
            with self._lock:
                self._reconciliation_failed = True
                self._last_error = "reconciliation parity or queue-drain failed"
            self._transition(RagRuntimeState.DEGRADED)
            return self.state
        with self._lock:
            self._reconciliation_failed = False
        self._transition(RagRuntimeState.CANONICAL)
        return self.state

    def fail_reconciliation(self, reason: str) -> RagRuntimeState:
        """A374: RECONCILING -> DEGRADED when reconciliation cannot complete.

        The service stays bounded at DEGRADED; the status surface reports
        the derived RECONCILIATION_FAILED outcome until the next attempt.
        """
        with self._lock:
            self._last_error = reason
            self._reconciliation_failed = True
        self._transition(RagRuntimeState.DEGRADED)
        return self.state

    # -- Degraded mutation enqueue -----------------------------------------

    def enqueue_degraded_mutation(
        self, *,
        module_id: str, resource_id: str, locator_id: str,
        source_revision: int, content_hash: str, operation: str,
        embedding_model: str, embedding_version: int = 1,
        chunk_size: int = 0, chunk_overlap: int = 0,
        chunking_version: int = 1, parser_version: int = 1,
        schema_version: int = 1, tombstone_generation: int = 0,
        correlation_id: Optional[str] = None, deadline: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> ReconciliationQueueItem:
        """A374: every degraded mutation must create a durable queue item."""
        self._tombstone.reject_stale_write(
            module_id=module_id,
            resource_id=resource_id,
            source_revision=source_revision,
            operation=operation,
        )
        item = _queue_item(
            operation_id=str(uuid.uuid4()),
            tombstone_generation=tombstone_generation
            or self._tombstone.current_generation(module_id, resource_id),
            correlation_id=correlation_id,
            deadline=deadline,
            payload=payload,
            identity=dict(
                module_id=module_id, resource_id=resource_id,
                locator_id=locator_id, source_revision=source_revision,
                content_hash=content_hash, operation=operation,
            ),
            versions=dict(
                embedding_model=embedding_model,
                embedding_version=embedding_version,
                chunk_size=chunk_size, chunk_overlap=chunk_overlap,
                chunking_version=chunking_version,
                parser_version=parser_version, schema_version=schema_version,
            ),
        )
        self._queue.enqueue(item)
        _logger.info(
            "RagRuntimeStateMachine: enqueued degraded %s for %s:%s revision=%s",
            operation, module_id, resource_id, source_revision,
        )
        return item

    @property
    def seconds_in_state(self) -> float:
        """Elapsed seconds since the last guarded transition (wall clock)."""
        with self._lock:
            try:
                entered = datetime.fromisoformat(self._last_transition_at)
            except ValueError:
                return 0.0
            return max(
                0.0, (datetime.now(timezone.utc) - entered).total_seconds()
            )

    # -- Status snapshot ----------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self._state.value,
                "effective_state": self.effective_state,
                "reconciliation_failed": self._reconciliation_failed,
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
