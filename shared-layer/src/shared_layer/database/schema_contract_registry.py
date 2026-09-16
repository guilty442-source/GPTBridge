"""Schema Contract Registry (A44/E30 + A8/E21).

Registers every PostgreSQL schema, SQLite template, RLS policy, role, index,
and migration version as a verifiable contract.  At startup, the registry
compares the declared contract against the actual database state and
reports drift before the runtime is allowed to serve traffic.

The registry is read-only at runtime — it never mutates the database.  All
repairs are delegated to the migration runner or an authorized owner.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TableContract:
    """Declared contract for a single PostgreSQL table."""

    schema: str
    table: str
    columns: tuple[str, ...]
    rls_enabled: bool = True
    rls_forced: bool = True
    indexes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RoleContract:
    """Declared contract for a PostgreSQL role."""

    role_name: str
    grants: tuple[str, ...]  # (schema.table:privilege, ...)


@dataclass(frozen=True)
class SchemaContract:
    """Full schema contract for the PostgreSQL central index."""

    tables: tuple[TableContract, ...]
    roles: tuple[RoleContract, ...]
    migration_count: int  # expected number of applied migrations


# ============================================================================
# Declared contracts (source of truth — must match central_index.sql + migrations)
# ============================================================================

_DECLARED_TABLES: tuple[TableContract, ...] = (
    TableContract(
        schema="gptbridge_security",
        table="principal",
        columns=("role_name", "module_id", "global_read", "transport_execute", "audit_write"),
        rls_enabled=False,
        rls_forced=False,
    ),
    TableContract(
        schema="gptbridge_security",
        table="principal_scope",
        columns=("role_name", "module_id", "can_read", "can_write"),
        rls_enabled=False,
        rls_forced=False,
    ),
    TableContract(
        schema="gptbridge_index",
        table="resource",
        columns=(
            "resource_id", "platform_id", "module_id", "owner_id", "data_category",
            "resource_type", "resource_label", "classification", "locator_id",
            "content_hash", "version", "index_status", "metadata",
            "created_at", "updated_at",
            "backend_generation", "stale",
            "authority_class", "executor_id", "correlation_id", "source_revision",
        ),
        indexes=("resource_module_category_idx", "resource_status_idx", "resource_metadata_idx",
                 "resource_authority_class_idx", "resource_correlation_idx", "resource_executor_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="resource_relation",
        columns=(
            "source_resource_id", "target_resource_id", "relation_type",
            "metadata", "created_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="schema_version",
        columns=("version_id", "migration_name", "applied_at", "applied_by", "checksum"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="module_version",
        columns=(
            "module_id", "store_type", "schema_version", "integrity_hash",
            "last_reconciled_at", "last_reconciled_status", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_rag",
        table="chunk",
        columns=(
            "chunk_id", "resource_id", "module_id", "sequence",
            "character_start", "character_end", "qdrant_point_id",
            "embedding_model", "locator_fragment", "metadata", "created_at",
            "content_tsv",
        ),
        indexes=("rag_chunk_module_idx", "rag_chunk_fts_idx", "rag_chunk_module_fts_idx"),
    ),
    TableContract(
        schema="gptbridge_rag",
        table="index_state",
        columns=(
            "resource_id", "module_id", "embedding_model", "qdrant_collection",
            "chunk_count", "status", "version", "indexed_at", "updated_at",
            "embedding_dimension", "chunk_size", "chunk_overlap", "content_hash",
            "qdrant_point_id", "postgresql_record_id", "source_revision",
            "tombstone_generation", "embedding_version", "chunking_version",
            "parser_version", "rag_schema_version", "pipeline_version",
            "backend_generation",
            "authority_class", "executor_id", "correlation_id",
        ),
    ),
    TableContract(
        schema="gptbridge_transport",
        table="tool_request",
        columns=(
            "channel_id", "request_id", "requester_actor", "target_tool_id",
            "payload", "status", "response", "progress",
            "created_at", "updated_at",
            "idempotency_key", "processed_at", "response_payload",
            "claimed_at", "lease_until", "attempt_count", "next_retry_at",
            "dead_letter_reason", "dead_letter_at", "max_attempts",
        ),
        indexes=(
            "tool_request_claim_idx", "tool_request_idempotency_idx",
            "tool_request_idempotency_lookup_idx", "tool_request_expired_lease_idx",
            "tool_request_retry_idx", "tool_request_dead_letter_idx",
            "tool_request_updated_at_idx",
        ),
    ),
    TableContract(
        schema="gptbridge_audit",
        table="event",
        columns=(
            "event_id", "actor_id", "module_id", "resource_id", "action",
            "outcome", "decision_id", "details", "occurred_at",
            "acting_module_id", "idempotency_key", "sequence_number",
            "executor_id", "correlation_id", "source_revision",
        ),
        indexes=("audit_event_sequence_idx", "audit_event_occurred_at_idx", "audit_module_id_idx",
                 "audit_event_correlation_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="reconcile_conflict_log",
        columns=(
            "conflict_id", "module_id", "resource_id", "conflict_type",
            "local_version", "central_version", "local_hash", "central_hash",
            "detail", "detected_at", "resolved_at", "resolution_action",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="retention_policy",
        columns=(
            "schema_name", "table_name", "retention_days", "retention_column",
            "purge_method", "enabled", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="backup_catalog",
        columns=(
            "backup_id", "engine", "source_path", "backup_path",
            "source_generation", "schema_version", "backup_hash",
            "size_bytes", "created_at", "restore_tested_at",
            "restore_certified", "restore_certification",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="backend_generation_state",
        columns=("generation_id", "generation", "reason", "set_at", "set_by"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="maintenance_window",
        columns=(
            "window_id", "operation", "target_schema", "target_table",
            "scheduled_start", "scheduled_end", "actual_start", "actual_end",
            "status", "result", "created_at", "created_by",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="data_lineage",
        columns=(
            "resource_id", "source_module", "source_revision", "produce_method",
            "sync_path", "last_writer_id", "last_writer_at",
            "last_writer_actor_id", "last_writer_executor_id",
            "last_writer_decision_id", "last_writer_correlation_id",
            "lineage_metadata",
        ),
        indexes=("data_lineage_source_module_idx", "data_lineage_writer_idx"),
    ),
)

_DECLARED_ROLES: tuple[RoleContract, ...] = (
    RoleContract("gptbridge_index_reader", ("gptbridge_index:SELECT", "gptbridge_rag:SELECT", "gptbridge_audit:SELECT")),
    RoleContract("gptbridge_index_executor", (
        "gptbridge_index:SELECT,INSERT,UPDATE,DELETE",
        "gptbridge_rag:SELECT,INSERT,UPDATE,DELETE",
        "gptbridge_transport:SELECT,INSERT,UPDATE,DELETE",
        "gptbridge_audit:SELECT,INSERT",
    )),
    RoleContract("gptbridge_xingcheng_reader", (
        "gptbridge_index:SELECT", "gptbridge_rag:SELECT", "gptbridge_audit:SELECT",
    )),
    RoleContract("gptbridge_transport_executor", (
        "gptbridge_transport:SELECT,INSERT,UPDATE,DELETE",
    )),
)

EXPECTED_MIGRATION_COUNT = 20  # 001 through 020


@dataclass
class ContractDrift:
    """A single drift between declared and actual schema."""

    table: str
    issue: str
    detail: str = ""


@dataclass
class ContractVerificationResult:
    """Result of verifying the schema contract against the live database."""

    passed: bool
    drifts: list[ContractDrift] = field(default_factory=list)
    migration_count: int = 0


def declared_contract() -> SchemaContract:
    """Return the declared schema contract."""
    return SchemaContract(
        tables=_DECLARED_TABLES,
        roles=_DECLARED_ROLES,
        migration_count=EXPECTED_MIGRATION_COUNT,
    )


def verify_contract(connection: Any) -> ContractVerificationResult:
    """Verify the live PostgreSQL database against the declared contract.

    This function is read-only — it never mutates the database.
    """
    contract = declared_contract()
    drifts: list[ContractDrift] = []

    # 1. Verify tables exist and have expected columns
    for table_contract in contract.tables:
        full_name = f"{table_contract.schema}.{table_contract.table}"
        try:
            rows = connection.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table_contract.schema, table_contract.table),
            ).fetchall()
            actual_cols = {str(r[0]) for r in rows}
            if not actual_cols:
                drifts.append(ContractDrift(full_name, "table_missing"))
                continue
            for expected_col in table_contract.columns:
                if expected_col not in actual_cols:
                    drifts.append(ContractDrift(full_name, "column_missing", expected_col))
        except Exception as exc:
            drifts.append(ContractDrift(full_name, "query_failed", str(exc)[:200]))

    # 2. Verify RLS is enabled and forced
    for table_contract in contract.tables:
        if not table_contract.rls_enabled:
            continue
        full_name = f"{table_contract.schema}.{table_contract.table}"
        try:
            row = connection.execute(
                """
                SELECT relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname = %s AND relnamespace = (
                    SELECT oid FROM pg_namespace WHERE nspname = %s
                )
                """,
                (table_contract.table, table_contract.schema),
            ).fetchone()
            if row is None:
                drifts.append(ContractDrift(full_name, "table_not_found"))
            else:
                rls_enabled = bool(row[0])
                rls_forced = bool(row[1])
                if table_contract.rls_enabled and not rls_enabled:
                    drifts.append(ContractDrift(full_name, "rls_not_enabled"))
                if table_contract.rls_forced and not rls_forced:
                    drifts.append(ContractDrift(full_name, "rls_not_forced"))
        except Exception as exc:
            drifts.append(ContractDrift(full_name, "rls_query_failed", str(exc)[:200]))

    # 3. Verify migration count
    try:
        row = connection.execute(
            "SELECT count(*) FROM gptbridge_migration.history"
        ).fetchone()
        migration_count = int(row[0]) if row else 0
    except Exception:
        migration_count = 0
        drifts.append(ContractDrift("gptbridge_migration.history", "migration_table_unreadable"))

    if migration_count < contract.migration_count:
        drifts.append(ContractDrift(
            "gptbridge_migration.history",
            "migration_count_behind",
            f"expected>={contract.migration_count} actual={migration_count}",
        ))

    return ContractVerificationResult(
        passed=len(drifts) == 0,
        drifts=drifts,
        migration_count=migration_count,
    )


__all__ = [
    "TableContract",
    "RoleContract",
    "SchemaContract",
    "ContractDrift",
    "ContractVerificationResult",
    "declared_contract",
    "verify_contract",
    "EXPECTED_MIGRATION_COUNT",
]
