"""SQL Governance Package — A500-A506 完整實作。

四大閉包：
1. SQL_AUTHORITY_CLOSURE           — PostgreSQL / SQLite 角色權限
2. SQL_SCHEMA_MIGRATION_CLOSURE    — Schema / Migration / Drift
3. INFORMATION_SQL_CLOSURE         — Transport / Audit / Outbox-Inbox
4. SQL_TRANSACTION_CONCURRENCY_CLOSURE — Transaction / Concurrency / Pool / Migration Execution

聚合：SQL_GOVERNANCE_CLOSURE = all four PASS
"""

from .sql_concurrency import (
    # Enums
    TransactionPolicy,
    ResourceClass,
    IsolationRequirement,
    RetryClassification,
    TransactionClass,
    SqlFault,
    # Mappings
    RESOURCE_CLASS_ISOLATION,
    FAULT_RETRY_CLASSIFICATION,
    # Core dataclasses
    TransactionBoundary,
    RevisionControl,
    PoolPolicy,
    SessionBinding,
    MigrationReceipt,
    OutboxContract,
    InboxDedup,
    # Schemas
    TransactionPolicySchema,
    IsolationPolicySchema,
    RevisionControlSchema,
    IdempotencyPolicySchema,
    PoolPolicySchema,
    SessionBindingSchema,
    MigrationExecutionSchema,
    OutboxContractSchema,
    InboxDedupSchema,
    ClosureEvidence,
    # Closures
    SqlAuthorityClosure,
    SqlSchemaMigrationClosure,
    InformationSqlClosure,
    SqlTransactionConcurrencyClosure,
    SqlGovernanceClosure,
)

from .transaction_boundary import (
    TransactionExecutor,
    declare_boundary,
    TransactionBoundaryError,
    IdempotencyCollisionError,
    ConcurrentModificationError,
    StaleRevisionError,
)

from .isolation import (
    IsolationEnforcer,
    IsolationLevelError,
    detect_resource_class,
    IsolationPolicyRegistry,
    DEFAULT_ISOLATION_REGISTRY,
)

from .idempotency import (
    IdempotencyRegistry,
    IdempotencyError,
    IdempotencyCollisionError,
    IDEMPOTENCY_REGISTRY_SQL,
    generate_idempotency_key,
)

from .revision import (
    RevisionController,
    RevisionError,
    StaleRevisionError,
    REVISION_TABLE_REQUIREMENTS,
    verify_no_lww_policy,
)

from .pool import (
    SecureConnectionPool,
    PoolMetrics,
    PoolSecurityError,
    SessionLeakError,
    ThreeLayerIdentity,
)

from .session import (
    SessionBinder,
    SessionContext,
    SessionBindingError,
    SESSION_VARIABLE_SETUP_SQL,
    SECURITY_PREDICATE_EXAMPLES,
)

from .migration import (
    MigrationExecutor,
    SchemaInspector,
    MigrationReceipt,
    MigrationError,
    MigrationLockError,
    MigrationReceiptMismatchError,
    MIGRATION_REGISTRY_SQL,
)

from .outbox_inbox import (
    TransactionalOutbox,
    OutboxContract,
    OutboxState,
    OutboxOperation,
    OutboxWorker,
    InboxDedupProcessor,
    InboxDedup,
    DuplicateMessageError,
    OUTBOX_TABLE_SQL,
    INBOX_DEDUP_TABLE_SQL,
)

from .schema_registry import (
    MachineSchemaRegistry,
    MACHINE_SCHEMA_REGISTRY,
    build_closure_evidence,
    evaluate_sql_governance_closure,
)

# Full SQL schema for all governance tables
FULL_SQL_GOVERNANCE_SCHEMA = """
-- ============================================================================
-- SQL Governance Tables (A500-A506)
-- ============================================================================

-- Idempotency Registry (A502)
CREATE TABLE IF NOT EXISTS gptbridge_rag.idempotency_registry (
    receipt_id TEXT PRIMARY KEY,
    owner_identity TEXT NOT NULL,
    operation_class TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    payload JSONB,
    status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','COMPLETED','FAILED')),
    result JSONB,
    error TEXT,
    created_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    UNIQUE (owner_identity, operation_class, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_idempotency_registry_lookup
ON gptbridge_rag.idempotency_registry (owner_identity, operation_class, idempotency_key);

-- Migration Receipts (A506)
CREATE TABLE IF NOT EXISTS gptbridge_rag.migration_receipts (
    receipt_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL,
    executor_identity TEXT NOT NULL,
    source_schema_hash TEXT NOT NULL,
    expected_target_hash TEXT NOT NULL,
    observed_target_hash TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    committed_at_utc TEXT,
    transaction_result TEXT NOT NULL DEFAULT 'PENDING' CHECK (transaction_result IN ('PENDING','COMMITTED','ROLLED_BACK')),
    verification_suite_result TEXT NOT NULL DEFAULT 'PENDING' CHECK (verification_suite_result IN ('PENDING','PASSED','FAILED')),
    rollback_policy TEXT NOT NULL DEFAULT 'AUTOMATIC_ON_FAILURE',
    previous_migration_receipt_hash TEXT,
    receipt_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_migration_receipts_migration
ON gptbridge_rag.migration_receipts (migration_id);

CREATE INDEX IF NOT EXISTS idx_migration_receipts_result
ON gptbridge_rag.migration_receipts (transaction_result);

-- Outbox Event (A506 - TRANSACTIONAL_OUTBOX)
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

-- Inbox Deduplication (A506 - INBOX_DEDUP)
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

-- Migration Receipts (A506)
CREATE TABLE IF NOT EXISTS gptbridge_rag.migration_receipts (
    receipt_id TEXT PRIMARY KEY,
    migration_id TEXT NOT NULL,
    executor_identity TEXT NOT NULL,
    source_schema_hash TEXT NOT NULL,
    expected_target_hash TEXT NOT NULL,
    observed_target_hash TEXT NOT NULL,
    started_at_utc TEXT NOT NULL,
    committed_at_utc TEXT,
    transaction_result TEXT NOT NULL DEFAULT 'PENDING' CHECK (transaction_result IN ('PENDING','COMMITTED','ROLLED_BACK')),
    verification_suite_result TEXT NOT NULL DEFAULT 'PENDING' CHECK (verification_suite_result IN ('PENDING','PASSED','FAILED')),
    rollback_policy TEXT NOT NULL DEFAULT 'AUTOMATIC_ON_FAILURE',
    previous_migration_receipt_hash TEXT,
    receipt_hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_migration_receipts_migration
ON gptbridge_rag.migration_receipts (migration_id);

CREATE INDEX IF NOT EXISTS idx_migration_receipts_result
ON gptbridge_rag.migration_receipts (transaction_result);

-- Updated at trigger for outbox
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

__all__ = [
    # sql_concurrency
    "TransactionPolicy",
    "ResourceClass",
    "IsolationRequirement",
    "RetryClassification",
    "TransactionClass",
    "SqlFault",
    "RESOURCE_CLASS_ISOLATION",
    "FAULT_RETRY_CLASSIFICATION",
    "TransactionBoundary",
    "RevisionControl",
    "PoolPolicy",
    "SessionBinding",
    "MigrationReceipt",
    "OutboxContract",
    "InboxDedup",
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
    "SqlAuthorityClosure",
    "SqlSchemaMigrationClosure",
    "InformationSqlClosure",
    "SqlTransactionConcurrencyClosure",
    "SqlGovernanceClosure",
    # transaction_boundary
    "TransactionExecutor",
    "declare_boundary",
    "TransactionBoundaryError",
    "IdempotencyCollisionError",
    "ConcurrentModificationError",
    "StaleRevisionError",
    # isolation
    "IsolationEnforcer",
    "IsolationLevelError",
    "detect_resource_class",
    "IsolationPolicyRegistry",
    "DEFAULT_ISOLATION_REGISTRY",
    # idempotency
    "IdempotencyRegistry",
    "IdempotencyError",
    "IdempotencyCollisionError",
    "IDEMPOTENCY_REGISTRY_SQL",
    "generate_idempotency_key",
    # revision
    "RevisionController",
    "RevisionError",
    "StaleRevisionError",
    "REVISION_TABLE_REQUIREMENTS",
    "verify_no_lww_policy",
    # pool
    "SecureConnectionPool",
    "PoolMetrics",
    "PoolSecurityError",
    "SessionLeakError",
    "ThreeLayerIdentity",
    # session
    "SessionBinder",
    "SessionContext",
    "SessionBindingError",
    "SESSION_VARIABLE_SETUP_SQL",
    "SECURITY_PREDICATE_EXAMPLES",
    # migration
    "MigrationExecutor",
    "SchemaInspector",
    "MigrationReceipt",
    "MigrationError",
    "MigrationLockError",
    "MigrationReceiptMismatchError",
    "MIGRATION_REGISTRY_SQL",
    # outbox_inbox
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
    # schema_registry
    "MachineSchemaRegistry",
    "MACHINE_SCHEMA_REGISTRY",
    "build_closure_evidence",
    "evaluate_sql_governance_closure",
    # Schema
    "FULL_SQL_GOVERNANCE_SCHEMA",
]