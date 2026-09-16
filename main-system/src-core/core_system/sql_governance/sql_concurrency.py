"""SQL Transaction Concurrency Governance — 核心定義與閉包結構。

A500-A506: 交易邊界、隔離級別、冪等性、版本控制、連接池安全、會話綁定、遷移執行器。

四大閉包：
1. SQL_AUTHORITY_CLOSURE           — PostgreSQL / SQLite 角色權限
2. SQL_SCHEMA_MIGRATION_CLOSURE    — Schema / Migration / Drift
3. INFORMATION_SQL_CLOSURE         — Transport / Audit / Outbox-Inbox
4. SQL_TRANSACTION_CONCURRENCY_CLOSURE — Transaction / Concurrency / Pool / Migration Execution

聚合：SQL_GOVERNANCE_CLOSURE = all four PASS
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


# ============================================================================
# A500: Transaction Policy Enum
# ============================================================================

class TransactionPolicy(str, Enum):
    """Allowed transaction policies per A500."""
    ATOMIC_SINGLE_DATABASE = "ATOMIC_SINGLE_DATABASE"       # One DB, all-or-nothing
    ATOMIC_WITH_OUTBOX = "ATOMIC_WITH_OUTBOX"               # DB + outbox in same transaction
    READ_ONLY_SNAPSHOT = "READ_ONLY_SNAPSHOT"               # Read-only, repeatable read
    BEST_EFFORT_NONAUTHORITATIVE = "BEST_EFFORT_NONAUTHORITATIVE"  # Non-canonical only


class ResourceClass(str, Enum):
    """Resource classification per A501 - determines isolation requirement."""
    SECURITY_PROJECTION = "SECURITY_PROJECTION"             # Permission, identity → SERIALIZABLE
    CENTRAL_OFFICIAL_DATA = "CENTRAL_OFFICIAL_DATA"         # Canonical data → REPEATABLE_READ/SERIALIZABLE
    AUDIT_APPEND = "AUDIT_APPEND"                           # Audit log → append + uniqueness
    TRANSPORT_DELIVERY = "TRANSPORT_DELIVERY"               # Outbox/inbox → row-level + idempotent
    DERIVED_CACHE = "DERIVED_CACHE"                         # Cache/derived → weaker allowed
    PRIVATE_STATE = "PRIVATE_STATE"                         # UI prefs → LWW allowed (explicit)


class IsolationRequirement(str, Enum):
    """PostgreSQL isolation levels mapped to resource class."""
    SERIALIZABLE = "SERIALIZABLE"
    REPEATABLE_READ = "REPEATABLE_READ"
    READ_COMMITTED = "READ_COMMITTED"
    # "WEAKER" for DERIVED_CACHE with explicit declaration


class RetryClassification(str, Enum):
    """Per A506 - deadlock/serialization failure handling."""
    SAFE_RETRY = "SAFE_RETRY"                       # Transient, no re-evaluation
    NON_RETRYABLE = "NON_RETRYABLE"                 # Constraint violation, auth failure
    REQUIRES_REEVALUATION = "REQUIRES_REEVALUATION" # Serialization failure - must re-read/re-adjudicate


class TransactionClass(str, Enum):
    """Transaction classification for DIR_DATA_SCHEMA_AUTHORITY per A501."""
    CANONICAL_WRITE = "CANONICAL_WRITE"
    CANONICAL_READ = "CANONICAL_READ"
    AUDIT_WRITE = "AUDIT_WRITE"
    OUTBOX_WRITE = "OUTBOX_WRITE"
    INBOX_PROCESS = "INBOX_PROCESS"
    MIGRATION_EXEC = "MIGRATION_EXEC"
    DERIVED_WRITE = "DERIVED_WRITE"
    PRIVATE_WRITE = "PRIVATE_WRITE"


# ============================================================================
# A500: Transaction Boundary Declaration
# ============================================================================

@dataclass(frozen=True)
class TransactionBoundary:
    """One governed operation = one declared transaction boundary (A500)."""
    operation_id: str
    transaction_policy: TransactionPolicy
    transaction_scope: tuple[str, ...]        # Tables/collections involved
    expected_side_effects: tuple[str, ...]    # "business_row", "audit_ref", "outbox_event", "transport_pub"
    commit_condition: str                     # e.g., "all_writes_succeeded"
    rollback_condition: str                   # e.g., "any_write_failed"
    resource_class: ResourceClass
    isolation_requirement: IsolationRequirement
    concurrency_policy: str                   # e.g., "optimistic_lock", "pessimistic_lock"
    locking_policy: str                       # e.g., "row_level", "advisory_lock"
    conflict_policy: str                      # e.g., "fail_on_conflict", "retry_with_backoff"
    idempotency_key: Optional[str] = None
    operation_class: Optional[str] = None

    def __post_init__(self):
        # A500: BEST_EFFORT forbidden for canonical data
        if (self.transaction_policy == TransactionPolicy.BEST_EFFORT_NONAUTHORITATIVE and
            self.resource_class in (ResourceClass.SECURITY_PROJECTION, ResourceClass.CENTRAL_OFFICIAL_DATA)):
            raise ValueError("BEST_EFFORT forbidden for canonical/official data")


# ============================================================================
# A501: Resource Class → Isolation Requirement Mapping
# ============================================================================

RESOURCE_CLASS_ISOLATION: dict[ResourceClass, IsolationRequirement] = {
    ResourceClass.SECURITY_PROJECTION: IsolationRequirement.SERIALIZABLE,
    ResourceClass.CENTRAL_OFFICIAL_DATA: IsolationRequirement.REPEATABLE_READ,
    ResourceClass.AUDIT_APPEND: IsolationRequirement.READ_COMMITTED,  # Append + unique constraint
    ResourceClass.TRANSPORT_DELIVERY: IsolationRequirement.READ_COMMITTED,  # Row-level + idempotent
    ResourceClass.DERIVED_CACHE: IsolationRequirement.READ_COMMITTED,  # Weaker, explicit
    ResourceClass.PRIVATE_STATE: IsolationRequirement.READ_COMMITTED,  # LWW allowed if explicit
}


# ============================================================================
# A503: Revision Control Base
# ============================================================================

@dataclass(frozen=True)
class RevisionControl:
    """Canonical mutable data revision control (A503)."""
    record_id: str
    revision: int
    expected_revision: int
    updated_fields: tuple[str, ...]
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def apply(self, new_revision: int) -> "RevisionControl":
        return RevisionControl(
            record_id=self.record_id,
            revision=new_revision,
            expected_revision=new_revision,
            updated_fields=self.updated_fields,
        )


# ============================================================================
# A504: Connection Pool Policy
# ============================================================================

@dataclass(frozen=True)
class PoolPolicy:
    """Connection pool security policy (A504)."""
    # Pool identity layer
    database_login_role: str
    pool_max_size: int
    pool_min_size: int

    # Reset requirements
    reset_transaction: bool = True
    reset_role: bool = True
    reset_search_path: bool = True
    reset_temp_settings: bool = True
    reset_request_identity: bool = True
    reset_correlation_id: bool = True
    reset_authorization_context: bool = True

    # Health monitoring
    max_idle_transaction_age_seconds: int = 30
    max_transaction_age_seconds: int = 300
    log_idle_in_transaction: bool = True
    log_long_running_transaction: bool = True


# ============================================================================
# A505: Session Binding
# ============================================================================

@dataclass(frozen=True)
class SessionBinding:
    """DB Session bound to process-generation identity (A505)."""
    requester_identity: str
    requester_generation: int
    active_role_identity: str
    permission_generation: int
    correlation_id: str
    operation_id: str
    authorized: bool = False  # Must match signed authorization projection

    # Three-layer identity (A505)
    database_login_role: str = ""      # Pool layer
    application_principal: str = ""    # Module/service layer
    requester_identity: str = ""       # Business actor layer


# ============================================================================
# A506: Migration Executor
# ============================================================================

@dataclass(frozen=True)
class MigrationReceipt:
    """SQL Migration Receipt (A506)."""
    receipt_id: str
    migration_id: str
    executor_identity: str
    source_schema_hash: str
    expected_target_hash: str
    observed_target_hash: str
    started_at_utc: str
    committed_at_utc: Optional[str] = None
    transaction_result: str = "PENDING"   # PENDING, COMMITTED, ROLLED_BACK
    verification_suite_result: str = "PENDING"
    rollback_policy: str = "AUTOMATIC_ON_FAILURE"
    previous_migration_receipt_hash: Optional[str] = None
    receipt_hash: str = ""

    def compute_hash(self) -> str:
        import hashlib
        content = f"{self.receipt_id}:{self.migration_id}:{self.executor_identity}:{self.source_schema_hash}:{self.expected_target_hash}:{self.observed_target_hash}:{self.started_at_utc}:{self.transaction_result}"
        return hashlib.sha256(content.encode()).hexdigest()[:64]


# ============================================================================
# Outbox/Inbox Contracts (A506)
# ============================================================================

@dataclass(frozen=True)
class OutboxContract:
    """Transactional Outbox - canonical write path component."""
    event_id: str
    request_id: str
    operation: str                          # UPSERT, DELETE, REINDEX, UPDATE_METADATA
    module_id: str
    resource_id: str
    source_version: int
    content_hash: str
    generation_id: str
    payload: Optional[dict[str, Any]] = None
    state: str = "PENDING"                  # PENDING, PROCESSING, SUCCEEDED, RETRY, DEAD_LETTER
    attempt_count: int = 0
    next_retry_at: Optional[str] = None
    last_error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: Optional[str] = None


@dataclass(frozen=True)
class InboxDedup:
    """Inbox deduplication for exactly-once effect."""
    message_id: str
    idempotency_key: str
    received_at: str
    processed_at: Optional[str] = None
    receipt: Optional[dict] = None
    duplicate: bool = False


# ============================================================================
# SQL Faults (A506)
# ============================================================================

class SqlFault(str, Enum):
    """Formalised SQL faults for governance audit."""
    TRANSACTION_PARTIAL_EFFECT = "SQL_TRANSACTION_PARTIAL_EFFECT"
    CONCURRENT_MODIFICATION = "SQL_CONCURRENT_MODIFICATION"
    SERIALIZATION_FAILURE = "SQL_SERIALIZATION_FAILURE"
    DEADLOCK = "SQL_DEADLOCK"
    LOCK_TIMEOUT = "SQL_LOCK_TIMEOUT"
    STALE_REVISION = "SQL_STALE_REVISION"
    IDEMPOTENCY_COLLISION = "SQL_IDEMPOTENCY_COLLISION"
    DUPLICATE_OPERATION_EFFECT = "SQL_DUPLICATE_OPERATION_EFFECT"
    POOL_SESSION_LEAK = "SQL_POOL_SESSION_LEAK"
    POOL_EXHAUSTED = "SQL_POOL_EXHAUSTED"
    IDLE_TRANSACTION = "SQL_IDLE_TRANSACTION"
    MIGRATION_LOCK_FAILURE = "SQL_MIGRATION_LOCK_FAILURE"
    PARALLEL_MIGRATION_ATTEMPT = "SQL_PARALLEL_MIGRATION_ATTEMPT"
    UNAUTHORIZED_DDL = "SQL_UNAUTHORIZED_DDL"
    MIGRATION_RECEIPT_MISMATCH = "SQL_MIGRATION_RECEIPT_MISMATCH"
    OUTBOX_STALLED = "SQL_OUTBOX_STALLED"
    INBOX_DEDUP_FAILURE = "SQL_INBOX_DEDUP_FAILURE"
    TRANSACTION_CONCURRENCY_V1 = "SQL_TRANSACTION_CONCURRENCY_V1"


FAULT_RETRY_CLASSIFICATION: dict[SqlFault, RetryClassification] = {
    SqlFault.DEADLOCK: RetryClassification.REQUIRES_REEVALUATION,
    SqlFault.SERIALIZATION_FAILURE: RetryClassification.REQUIRES_REEVALUATION,
    SqlFault.LOCK_TIMEOUT: RetryClassification.SAFE_RETRY,
    SqlFault.CONCURRENT_MODIFICATION: RetryClassification.REQUIRES_REEVALUATION,
    SqlFault.STALE_REVISION: RetryClassification.REQUIRES_REEVALUATION,
    SqlFault.POOL_EXHAUSTED: RetryClassification.SAFE_RETRY,
    SqlFault.MIGRATION_LOCK_FAILURE: RetryClassification.NON_RETRYABLE,
    SqlFault.UNAUTHORIZED_DDL: RetryClassification.NON_RETRYABLE,
    SqlFault.IDEMPOTENCY_COLLISION: RetryClassification.NON_RETRYABLE,
    SqlFault.OUTBOX_STALLED: RetryClassification.SAFE_RETRY,
    SqlFault.INBOX_DEDUP_FAILURE: RetryClassification.NON_RETRYABLE,
}


# ============================================================================
# Machine Schemas & Closure Evidence
# ============================================================================

@dataclass(frozen=True)
class TransactionPolicySchema:
    """Machine schema for transaction_policy."""
    schema_code: str = "TRANSACTION_POLICY_V1"
    schema_kind: str = "machine"
    semantic_owner: str = "SQL_TRANSACTION_CONCURRENCY"
    required_fields: tuple[str, ...] = (
        "operation_id", "transaction_policy", "transaction_scope",
        "expected_side_effects", "commit_condition", "rollback_condition",
        "resource_class", "isolation_requirement", "concurrency_policy",
        "locking_policy", "conflict_policy"
    )
    status: str = "active"


@dataclass(frozen=True)
class IsolationPolicySchema:
    """Machine schema for isolation policy by resource class."""
    schema_code: str = "ISOLATION_POLICY_V1"
    schema_kind: str = "machine"
    semantic_owner: str = "SQL_TRANSACTION_CONCURRENCY"
    required_fields: tuple[str, ...] = (
        "resource_class", "isolation_requirement", "concurrency_policy",
        "locking_policy", "conflict_policy"
    )
    status: str = "active"


@dataclass(frozen=True)
class RevisionControlSchema:
    """Machine schema for revision control."""
    schema_code: str = "REVISION_CONTROL_V1"
    schema_kind: str = "machine"
    semantic_owner: str = "SQL_TRANSACTION_CONCURRENCY"
    required_fields: tuple[str, ...] = (
        "record_id", "revision", "expected_revision", "updated_fields"
    )
    status: str = "active"


@dataclass(frozen=True)
class IdempotencyPolicySchema:
    """Machine schema for idempotency."""
    schema_code: str = "IDEMPOTENCY_POLICY_V1"
    schema_kind: str = "machine"
    semantic_owner: str = "SQL_TRANSACTION_CONCURRENCY"
    required_fields: tuple[str, ...] = (
        "operation_id", "idempotency_key", "owner_identity",
        "operation_class", "unique_constraint"
    )
    status: str = "active"


@dataclass(frozen=True)
class PoolPolicySchema:
    """Machine schema for pool policy."""
    schema_code: str = "POOL_POLICY_V1"
    schema_kind: str = "machine"
    semantic_owner: str = "SQL_TRANSACTION_CONCURRENCY"
    required_fields: tuple[str, ...] = (
        "database_login_role", "reset_transaction", "reset_role",
        "reset_search_path", "reset_temp_settings", "reset_request_identity",
        "reset_correlation_id", "reset_authorization_context",
        "max_idle_transaction_age_seconds", "max_transaction_age_seconds"
    )
    status: str = "active"


@dataclass(frozen=True)
class SessionBindingSchema:
    """Machine schema for session binding."""
    schema_code: str = "SESSION_BINDING_V1"
    schema_kind: str = "machine"
    semantic_owner: str = "SQL_TRANSACTION_CONCURRENCY"
    required_fields: tuple[str, ...] = (
        "requester_identity", "requester_generation", "active_role_identity",
        "permission_generation", "correlation_id", "operation_id",
        "database_login_role", "application_principal"
    )
    status: str = "active"


@dataclass(frozen=True)
class MigrationExecutionSchema:
    """Machine schema for migration execution."""
    schema_code: str = "MIGRATION_EXECUTION_V1"
    schema_kind: str = "machine"
    semantic_owner: str = "SQL_SCHEMA_MIGRATION"
    required_fields: tuple[str, ...] = (
        "migration_id", "executor_identity", "source_schema_hash",
        "expected_target_hash", "observed_target_hash", "migration_lock_acquired",
        "receipt_id", "verification_suite_result"
    )
    status: str = "active"


@dataclass(frozen=True)
class OutboxContractSchema:
    """Machine schema for outbox contract."""
    schema_code: str = "OUTBOX_CONTRACT_V1"
    schema_kind: str = "machine"
    semantic_owner: str = "INFORMATION_SQL"
    required_fields: tuple[str, ...] = (
        "event_id", "request_id", "operation", "module_id", "resource_id",
        "source_version", "content_hash", "generation_id", "state",
        "attempt_count", "created_at", "updated_at", "completed_at"
    )
    status: str = "active"


@dataclass(frozen=True)
class InboxDedupSchema:
    """Machine schema for inbox deduplication."""
    schema_code: str = "INBOX_DEDUP_V1"
    schema_kind: str = "machine"
    semantic_owner: str = "INFORMATION_SQL"
    required_fields: tuple[str, ...] = (
        "message_id", "idempotency_key", "received_at",
        "processed_at", "receipt", "duplicate"
    )
    status: str = "active"


@dataclass(frozen=True)
class ClosureEvidence:
    """Closure evidence for SQL governance (A506 final)."""
    closure_id: str
    codex_version: str

    # Component hashes
    transaction_policy_hash: str
    isolation_policy_hash: str
    revision_control_hash: str
    idempotency_policy_hash: str
    pool_policy_hash: str
    session_binding_hash: str
    migration_execution_hash: str
    migration_receipt_chain_hash: str
    outbox_contract_hash: str
    inbox_contract_hash: str

    # Test evidence
    test_evidence_hash: str
    open_findings: list[str] = field(default_factory=list)
    result: str = "PENDING"  # PASS / FAIL / PENDING
    verified_at_utc: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # Overall
    def all_pass(self) -> bool:
        return self.result == "PASS"


# Four Closures
@dataclass(frozen=True)
class SqlAuthorityClosure:
    """SQL_AUTHORITY_CLOSURE — PostgreSQL / SQLite role permissions."""
    closure_id: str = "SQL_AUTHORITY_CLOSURE"
    result: str = "PENDING"
    evidence: Optional[ClosureEvidence] = None


@dataclass(frozen=True)
class SqlSchemaMigrationClosure:
    """SQL_SCHEMA_MIGRATION_CLOSURE — schema / migration / drift."""
    closure_id: str = "SQL_SCHEMA_MIGRATION_CLOSURE"
    result: str = "PENDING"
    evidence: Optional[ClosureEvidence] = None


@dataclass(frozen=True)
class InformationSqlClosure:
    """INFORMATION_SQL_CLOSURE — transport / audit / outbox-inbox."""
    closure_id: str = "INFORMATION_SQL_CLOSURE"
    result: str = "PENDING"
    evidence: Optional[ClosureEvidence] = None


@dataclass(frozen=True)
class SqlTransactionConcurrencyClosure:
    """SQL_TRANSACTION_CONCURRENCY_CLOSURE — transaction / concurrency / pool / migration execution."""
    closure_id: str = "SQL_TRANSACTION_CONCURRENCY_CLOSURE"
    result: str = "PENDING"
    evidence: Optional[ClosureEvidence] = None


@dataclass(frozen=True)
class SqlGovernanceClosure:
    """SQL_GOVERNANCE_CLOSURE = all four PASS."""
    authority: SqlAuthorityClosure
    schema_migration: SqlSchemaMigrationClosure
    information: InformationSqlClosure
    transaction_concurrency: SqlTransactionConcurrencyClosure

    def overall_result(self) -> str:
        return "PASS" if all(
            c.result == "PASS"
            for c in [self.authority, self.schema_migration, self.information, self.transaction_concurrency]
        ) else "FAIL"


__all__ = [
    # Enums
    "TransactionPolicy",
    "ResourceClass",
    "IsolationRequirement",
    "RetryClassification",
    "TransactionClass",
    "SqlFault",
    # Mappings
    "RESOURCE_CLASS_ISOLATION",
    "FAULT_RETRY_CLASSIFICATION",
    # Dataclasses
    "TransactionBoundary",
    "RevisionControl",
    "PoolPolicy",
    "SessionBinding",
    "MigrationReceipt",
    "OutboxContract",
    "InboxDedup",
    # Schemas
    "TransactionPolicySchema",
    "IsolationPolicySchema",
    "RevisionControlSchema",
    "IdempotencyPolicySchema",
    "PoolPolicySchema",
    "SessionBindingSchema",
    "MigrationExecutionSchema",
    "OutboxContractSchema",
    "InboxDedupSchema",
    "ClosureEvidence",
    # Closures
    "SqlAuthorityClosure",
    "SqlSchemaMigrationClosure",
    "InformationSqlClosure",
    "SqlTransactionConcurrencyClosure",
    "SqlGovernanceClosure",
]