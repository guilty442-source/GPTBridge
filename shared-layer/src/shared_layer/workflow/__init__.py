"""Cross-engine workflow (Saga) layer.

No distributed transactions:
single-engine work is ACID; multi-engine work is a durable, resumable
workflow with PostgreSQL as the operation authority.

  * operation / step state with leases and checkpoints,
  * idempotent steps + operation fingerprints,
  * fixed compensation table (rollback / compensate / invalidate /
    supersede / reconcile / append-only),
  * transactional outbox + inbox dedup (at-least-once + idempotent),
  * publish barrier (READY is the only readable state),
  * atomic file writes and tombstone deletes,
  * SQLite fallback saga + non-timestamp conflict policy.
"""

from .atomic_file import (
    AtomicFileError,
    FileCommit,
    file_sha256,
    remove_committed,
    stage_tombstone,
    verify_file,
    write_atomic,
)
from .barrier import (
    BarrierError,
    ConsistencyState,
    EngineEvidence,
    PublishBarrier,
    READABLE_STATES,
    ResourceState,
    evaluate_consistency,
)
from .fallback import (
    AUTHORITY_ORDER,
    ConflictFacts,
    ConflictResolution,
    FallbackSagaError,
    LocalOperation,
    LocalOperationStatus,
    resolve_conflict,
)
from .fingerprint import is_same_operation, operation_hash, payload_hash
from .operation import Operation, OperationLease, OperationStateError
from .outbox import (
    DELIVERY_CONTRACT,
    INBOX_INSERT_SQL,
    INBOX_LOOKUP_SQL,
    OUTBOX_ENQUEUE_SQL,
    OUTBOX_FETCH_SQL,
    Inbox,
    InboxEntry,
    OutboxEvent,
    deduplicated_result,
)
from .saga import SagaExecutor, SagaOutcome, StepHandler
from .steps import COMPENSATION_TABLE, RAG_INGEST_PLAN, StepPlan, StepResult, StepSpec
from .transaction_guard import (
    FORBIDDEN_IN_TRANSACTION,
    TransactionBoundaryError,
    assert_activity_allowed,
    assert_short_transaction,
)
from .types import (
    LOCAL_ONLY_TELEMETRY,
    SAGA_EVENTS,
    TERMINAL_STATUSES,
    Engine,
    OperationFacts,
    OperationStatus,
    OutcomeStrategy,
    StepStatus,
)

__all__ = [
    "AUTHORITY_ORDER",
    "AtomicFileError",
    "BarrierError",
    "COMPENSATION_TABLE",
    "ConflictFacts",
    "ConflictResolution",
    "ConsistencyState",
    "DELIVERY_CONTRACT",
    "Engine",
    "EngineEvidence",
    "FORBIDDEN_IN_TRANSACTION",
    "FallbackSagaError",
    "FileCommit",
    "INBOX_INSERT_SQL",
    "INBOX_LOOKUP_SQL",
    "Inbox",
    "InboxEntry",
    "LOCAL_ONLY_TELEMETRY",
    "LocalOperation",
    "LocalOperationStatus",
    "OUTBOX_ENQUEUE_SQL",
    "OUTBOX_FETCH_SQL",
    "Operation",
    "OperationFacts",
    "OperationLease",
    "OperationStateError",
    "OperationStatus",
    "OutboxEvent",
    "OutcomeStrategy",
    "PublishBarrier",
    "RAG_INGEST_PLAN",
    "READABLE_STATES",
    "ResourceState",
    "SAGA_EVENTS",
    "SagaExecutor",
    "SagaOutcome",
    "StepHandler",
    "StepPlan",
    "StepResult",
    "StepSpec",
    "StepStatus",
    "TERMINAL_STATUSES",
    "TransactionBoundaryError",
    "assert_activity_allowed",
    "assert_short_transaction",
    "deduplicated_result",
    "evaluate_consistency",
    "file_sha256",
    "is_same_operation",
    "operation_hash",
    "payload_hash",
    "remove_committed",
    "resolve_conflict",
    "stage_tombstone",
    "verify_file",
    "write_atomic",
]
