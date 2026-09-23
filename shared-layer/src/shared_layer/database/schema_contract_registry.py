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
            "deletion_stage", "tombstoned_at", "purge_after",
        ),
        indexes=("resource_module_category_idx", "resource_status_idx", "resource_metadata_idx",
                 "resource_authority_class_idx", "resource_correlation_idx", "resource_executor_idx",
                 "resource_deletion_stage_idx"),
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
            "deletion_stage",
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
    TableContract(
        schema="gptbridge_audit",
        table="permission_snapshot",
        columns=(
            "snapshot_id", "event_id", "actor_id", "session_user",
            "target_module", "target_resource_id", "target_classification",
            "evaluated_roles", "evaluated_policies", "decision_summary",
            "rls_context", "can_read", "can_write", "can_write_resource",
            "captured_at",
        ),
        indexes=("permission_snapshot_event_idx", "permission_snapshot_actor_idx", "permission_snapshot_module_idx"),
    ),
    TableContract(
        schema="gptbridge_audit",
        table="ddl_event",
        columns=(
            "ddl_event_id", "command_tag", "object_identity", "object_type",
            "schema_name", "object_name", "session_user",
            "migration_executor", "statement_hash", "occurred_at",
        ),
        indexes=("ddl_event_occurred_idx", "ddl_event_object_idx", "ddl_event_tag_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="contract_version",
        columns=(
            "contract_name", "current_version", "min_compatible_version",
            "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="sqlite_generation",
        columns=(
            "module_id", "database_path", "backend_generation",
            "last_synced_at", "stale", "updated_at",
        ),
        indexes=("sqlite_generation_stale_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="qdrant_generation",
        columns=(
            "collection_name", "backend_generation",
            "last_synced_at", "stale", "updated_at",
        ),
        indexes=("qdrant_generation_stale_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="workload_class",
        columns=(
            "class_name", "pool_owner", "statement_timeout_ms",
            "lock_timeout_ms", "priority", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="rebuild_certification",
        columns=(
            "certification_id", "engine", "target", "rebuild_reason",
            "checks_performed", "check_count", "passed_count", "certified",
            "resource_count", "content_hash", "schema_version",
            "rls_verified", "locator_verified", "certified_at", "certified_by",
        ),
        indexes=("rebuild_cert_engine_idx", "rebuild_cert_target_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="long_transaction_watchdog",
        columns=(
            "watchdog_id", "pid", "session_user", "state", "query",
            "transaction_age_seconds", "idle_in_transaction_seconds",
            "lock_holder", "threshold_seconds", "action_taken", "detected_at",
        ),
        indexes=("watchdog_detected_idx", "watchdog_pid_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="bloat_report",
        columns=(
            "bloat_id", "schema_name", "table_name",
            "estimated_bloat_percent", "dead_tuples", "live_tuples",
            "last_autovacuum", "autovacuum_count", "last_analyze",
            "table_size_bytes", "index_size_bytes", "collected_at",
        ),
        indexes=("bloat_collected_idx", "bloat_table_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="rpo_rto_class",
        columns=(
            "engine", "rpo_seconds", "rto_seconds",
            "backup_frequency_seconds", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="capacity_threshold",
        columns=(
            "metric_name", "warning_level", "critical_level",
            "fail_closed_level", "unit", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="readonly_domain",
        columns=(
            "domain_name", "is_readonly", "reason",
            "activated_at", "activated_by", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="startup_certification",
        columns=(
            "certification_id", "schema_version_verified", "rls_verified",
            "required_roles_verified", "migration_head_verified",
            "audit_append_only_verified", "authority_contract_verified",
            "contract_version_verified", "ready", "checks",
            "certified_at", "certified_by",
        ),
        indexes=("startup_cert_at_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="slo_metric",
        columns=(
            "metric_name", "target_value", "target_direction",
            "unit", "window_seconds", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="slo_observation",
        columns=(
            "observation_id", "metric_name", "observed_value",
            "met_target", "observed_at",
        ),
        indexes=("slo_obs_metric_idx", "slo_obs_at_idx"),
    ),
    # Phase E: performance and observability
    TableContract(
        schema="gptbridge_index",
        table="workload_pool_config",
        columns=(
            "pool_name", "max_connections", "max_open", "max_idle",
            "wait_timeout_seconds", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="query_class",
        columns=(
            "class_name", "pool_name", "statement_timeout_ms",
            "lock_timeout_ms", "retry_limit", "batch_size",
            "priority", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="query_fingerprint",
        columns=(
            "fingerprint_id", "query_key", "query_hash",
            "execution_count", "total_latency_ms", "mean_latency_ms",
            "p95_latency_ms", "rows_returned_total", "rows_scanned_total",
            "shared_blocks_hit_total", "shared_blocks_read_total",
            "last_executed_at", "first_seen_at", "updated_at",
        ),
        indexes=("query_fp_key_idx", "query_fp_latency_idx", "query_fp_count_idx"),
    ),
    TableContract(
        schema="gptbridge_transport",
        table="tool_request_history",
        columns=(
            "request_id", "channel_id", "target_tool_id", "status",
            "archived_at",
        ),
        indexes=("tool_request_history_archived_idx", "tool_request_history_status_idx"),
    ),
    TableContract(
        schema="gptbridge_audit",
        table="event_history",
        columns=(
            "event_id", "event_type", "actor", "occurred_at",
            "archived_at",
        ),
        indexes=("event_history_archived_idx", "event_history_occurred_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="partition_threshold",
        columns=(
            "table_schema", "table_name", "row_count_threshold",
            "size_mb_threshold", "latency_ms_threshold",
            "partition_enabled", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="wal_checkpoint_snapshot",
        columns=(
            "snapshot_id", "wal_size_bytes", "checkpoint_count",
            "checkpoint_duration_ms", "checkpoint_buffers_written",
            "checkpoint_sync_time_ms", "wal_segments_count",
            "wal_rate_mb_per_min", "collected_at",
        ),
        indexes=("wal_snap_collected_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="sqlite_database_class",
        columns=(
            "module_id", "database_path", "db_class",
            "synchronous_setting", "backup_frequency_seconds",
            "integrity_check_frequency_seconds", "retention_days",
            "reconcile_required", "description", "updated_at",
        ),
        indexes=("sqlite_class_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="reconcile_pending_queue",
        columns=(
            "queue_id", "module_id", "resource_id", "source_revision",
            "dirty", "enqueued_at", "last_reconciled_revision",
            "last_reconciled_at", "reconcile_attempts",
        ),
        indexes=("reconcile_queue_dirty_idx", "reconcile_queue_module_idx",
                 "reconcile_queue_resource_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="performance_baseline",
        columns=(
            "baseline_id", "operation_name", "p50_latency_ms",
            "p95_latency_ms", "p99_latency_ms", "sample_count",
            "baseline_at", "description",
        ),
        indexes=("perf_baseline_op_idx",),
    ),
    # Phase F: database release management
    TableContract(
        schema="gptbridge_index",
        table="database_release",
        columns=(
            "release_id", "schema_version", "migration_head",
            "rls_version", "role_version", "sqlite_template_version",
            "reconcile_contract_version", "qdrant_contract_version",
            "query_contract_version", "backup_format_version",
            "minimum_runtime_version", "compatibility_range",
            "state", "certification_result", "previous_release_id",
            "created_at", "activated_at", "superseded_at", "created_by",
        ),
        indexes=("db_release_state_idx", "db_release_previous_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="release_compatibility",
        columns=(
            "release_id", "runtime_version", "mode", "reason", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="migration_classification",
        columns=(
            "migration_id", "migration_name", "change_type",
            "pre_migration", "data_transform", "compatibility_window_days",
            "post_migration", "rollback_plan", "recovery_plan",
            "classified_at", "classified_by",
        ),
        indexes=("migration_class_type_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="query_contract",
        columns=(
            "contract_name", "version", "full_name", "sql_template",
            "status", "deprecated_at", "retired_at", "successor_version",
            "introduced_in_release", "retired_in_release",
            "description", "updated_at",
        ),
        indexes=("query_contract_name_idx", "query_contract_status_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="rls_role_migration",
        columns=(
            "migration_id", "rls_role_version", "change_type",
            "target_object", "change_sql", "rollback_sql",
            "introduced_in_release", "applied_at", "applied_by",
        ),
        indexes=("rls_role_mig_version_idx", "rls_role_mig_type_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="sqlite_template_release",
        columns=(
            "template_version", "schema_version", "minimum_reader_version",
            "minimum_writer_version", "ddl_hash", "introduced_in_release",
            "status", "deprecated_at", "retired_at", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="qdrant_contract",
        columns=(
            "contract_version", "collection_name", "vector_dimension",
            "distance_metric", "embedding_model", "payload_schema",
            "required_module_id", "resource_id_format", "chunk_id_format",
            "revision_field", "introduced_in_release", "status",
            "deprecated_at", "retired_at", "successor_version",
            "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="canary_upgrade",
        columns=(
            "canary_id", "release_id", "source_backup_id",
            "canary_database_name", "started_at", "completed_at",
            "status", "certification_id", "schema_hash",
            "failure_reason", "promoted_to_production", "promoted_at",
        ),
        indexes=("canary_release_idx", "canary_status_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="database_release_audit",
        columns=(
            "audit_id", "release_id", "previous_release_id",
            "migration_set", "executor", "started_at", "completed_at",
            "backup_id", "certification_id", "schema_hash",
            "result", "failure_reason", "audited_at",
        ),
        indexes=("release_audit_release_idx", "release_audit_result_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="roll_forward_migration",
        columns=(
            "corrective_migration_id", "fixes_migration_id", "fixes_release_id",
            "description", "corrective_sql", "verification_sql",
            "applied_at", "applied_by", "verified", "verified_at",
        ),
        indexes=("roll_forward_fixes_idx",),
    ),
    # Phase G: data lifecycle management
    TableContract(
        schema="gptbridge_index",
        table="lifecycle_state",
        columns=(
            "entity_type", "entity_id", "lifecycle_state",
            "previous_state", "reason", "transitioned_at", "transitioned_by",
        ),
        indexes=("lifecycle_state_idx", "lifecycle_state_transitioned_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="transport_retention_policy",
        columns=(
            "status", "hot_retention_days", "archive_after_days",
            "can_purge", "purge_after_days", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="audit_retention_layer",
        columns=(
            "layer_name", "retention_days", "next_layer",
            "compression_enabled", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="sqlite_retention_policy",
        columns=(
            "db_class", "retention_days", "archive_eligible",
            "archive_after_days", "purge_eligible", "purge_after_days",
            "version_history_required", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="qdrant_vector_lifecycle",
        columns=(
            "resource_id", "collection_name", "point_id", "vector_state",
            "resource_lifecycle_state", "pg_marked_at", "qdrant_deleted_at",
            "verified_at", "pg_confirmed_at", "deletion_reason", "updated_at",
        ),
        indexes=("qdrant_vec_lc_state_idx", "qdrant_vec_lc_collection_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="purge_queue",
        columns=(
            "queue_id", "resource_id", "module_id", "entity_type",
            "requested_at", "retention_until", "reason", "requested_by",
            "approval_id", "purge_status", "purged_at", "purge_audit_id",
            "updated_at",
        ),
        indexes=("purge_queue_status_idx", "purge_queue_resource_idx",
                 "purge_queue_retention_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="archive_catalog",
        columns=(
            "archive_id", "source_engine", "source_schema", "source_table",
            "module_id", "time_range_start", "time_range_end",
            "record_count", "schema_version", "release_id",
            "storage_locator", "storage_format", "integrity_hash",
            "encoding", "archive_format_version", "checksum_algorithm",
            "restore_tested_at", "restore_test_result",
            "created_at", "verified_at", "verification_result", "description",
        ),
        indexes=("archive_catalog_source_idx", "archive_catalog_time_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="retention_hold",
        columns=(
            "hold_id", "entity_type", "entity_id", "hold_reason",
            "hold_description", "hold_active", "placed_by", "placed_at",
            "released_by", "released_at", "expected_release_at", "updated_at",
        ),
        indexes=("retention_hold_entity_idx", "retention_hold_active_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="dependency_check",
        columns=(
            "check_id", "resource_id", "checked_at", "checked_by",
            "has_dependencies", "dependency_details", "can_purge",
            "purge_queue_id",
        ),
        indexes=("dependency_check_resource_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="archive_restore_test",
        columns=(
            "test_id", "archive_id", "tested_at", "tested_by",
            "temporary_database", "schema_check_passed", "row_count_match",
            "hash_verify_passed", "query_test_passed",
            "expected_record_count", "actual_record_count",
            "expected_hash", "actual_hash", "test_result",
            "overall_passed", "failure_reason",
        ),
        indexes=("archive_restore_test_idx", "archive_restore_passed_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="capacity_quota",
        columns=(
            "domain_name", "source_schema", "source_table",
            "soft_limit_mb", "hard_limit_mb", "archive_threshold_mb",
            "emergency_threshold_mb", "current_size_mb", "current_row_count",
            "last_measured_at", "description", "updated_at",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="purge_audit_log",
        columns=(
            "purge_id", "resource_id", "entity_type", "module_id",
            "actor", "executor", "approval_id", "purge_queue_id",
            "previous_hash", "deleted_from", "deleted_from_detail",
            "deleted_at", "verification_result", "verified", "verified_at",
            "audit_hash", "created_at",
        ),
        indexes=("purge_audit_resource_idx", "purge_audit_executor_idx",
                 "purge_audit_date_idx"),
    ),
    # Phase H: integrity verification
    TableContract(
        schema="gptbridge_index",
        table="reconcile_batch_digest",
        columns=(
            "reconcile_run_id", "module_id", "source_generation",
            "first_revision", "last_revision", "record_count",
            "batch_hash", "result_hash", "status", "started_at",
            "completed_at", "verified_at", "failure_reason",
            "previous_run_id",
        ),
        indexes=("reconcile_digest_module_idx", "reconcile_digest_status_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="resource_content_hash",
        columns=(
            "resource_id", "resource_hash", "metadata_hash", "locator_hash",
            "revision", "hash_algorithm", "computed_at", "verified_at",
            "tamper_state",
        ),
    ),
    TableContract(
        schema="gptbridge_index",
        table="sqlite_database_digest",
        columns=(
            "digest_id", "module_id", "database_path", "schema_hash",
            "revision_head", "row_count", "critical_table_digest",
            "generation", "computed_at", "verified_at", "tamper_state",
        ),
        indexes=("sqlite_digest_module_idx", "sqlite_digest_tamper_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="qdrant_integrity_map",
        columns=(
            "chunk_id", "resource_id", "chunk_hash", "embedding_version",
            "qdrant_point_id", "resource_revision", "pg_recorded_at",
            "qdrant_verified_at", "qdrant_point_exists", "hash_match",
            "version_match", "integrity_state", "updated_at",
        ),
        indexes=("qdrant_integrity_state_idx", "qdrant_integrity_resource_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="merkle_root",
        columns=(
            "merkle_id", "domain", "domain_id", "leaf_count", "merkle_root",
            "leaf_hashes", "computed_at", "verified_at", "tamper_state",
        ),
        indexes=("merkle_domain_idx", "merkle_tamper_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="integrity_snapshot",
        columns=(
            "snapshot_id", "database_generation", "schema_hash",
            "audit_head_hash", "resource_merkle_root", "migration_head",
            "sqlite_digest_count", "qdrant_integrity_count",
            "reconcile_batch_count", "release_id", "backup_id",
            "created_at", "verified_at", "tamper_state",
        ),
        indexes=("integrity_snapshot_gen_idx", "integrity_snapshot_tamper_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="restore_verification",
        columns=(
            "verification_id", "snapshot_id", "restored_at", "restored_by",
            "target_database", "expected_schema_hash", "actual_schema_hash",
            "expected_audit_head_hash", "actual_audit_head_hash",
            "expected_resource_merkle_root", "actual_resource_merkle_root",
            "expected_generation", "actual_generation",
            "expected_migration_head", "actual_migration_head",
            "schema_match", "audit_match", "merkle_match",
            "generation_match", "migration_match",
            "overall_passed", "failure_reason", "verified_at",
        ),
        indexes=("restore_verify_date_idx", "restore_verify_passed_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="tamper_state_registry",
        columns=(
            "state_id", "entity_type", "entity_id", "tamper_state",
            "detected_at", "detected_by", "details", "resolved_at",
            "resolved_by", "resolution_notes", "updated_at",
        ),
        indexes=("tamper_state_entity_idx", "tamper_state_state_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="fail_closed_action",
        columns=(
            "action_id", "trigger_type", "trigger_entity_id",
            "trigger_details", "action_taken", "domain", "triggered_at",
            "triggered_by", "released_at", "released_by",
            "release_reason", "active",
        ),
        indexes=("fail_closed_active_idx", "fail_closed_domain_idx"),
    ),
    # Phase I: dependency & version governance
    TableContract(
        schema="gptbridge_index",
        table="version_lock",
        columns=(
            "lock_id", "release_id", "component", "major_version",
            "minor_version", "patch_version", "version_string",
            "locked_at", "locked_by", "description",
        ),
        indexes=("version_lock_release_idx", "version_lock_component_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="compatibility_matrix_ext",
        columns=(
            "matrix_id", "release_id", "postgresql_version",
            "psycopg_version", "sqlite_runtime_version",
            "qdrant_server_version", "qdrant_client_version",
            "python_version", "status", "tested_at", "tested_by",
            "test_result", "notes", "updated_at",
        ),
        indexes=("compat_matrix_ext_release_idx", "compat_matrix_ext_status_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="upgrade_classification",
        columns=(
            "classification_id", "component", "from_version", "to_version",
            "upgrade_class", "required_validation", "allows_unattended",
            "requires_backup", "requires_clone_test",
            "requires_certification", "rollback_allowed",
            "description", "classified_by", "classified_at",
        ),
        indexes=("upgrade_class_component_idx", "upgrade_class_class_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="driver_compatibility_test",
        columns=(
            "test_id", "driver_name", "driver_version", "test_category",
            "passed", "tested_at", "tested_by", "test_details",
            "failure_reason", "duration_ms",
        ),
        indexes=("driver_compat_driver_idx", "driver_compat_passed_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="pg_major_upgrade_rehearsal",
        columns=(
            "rehearsal_id", "from_version", "to_version", "status",
            "started_at", "completed_at", "backup_id", "clone_database",
            "migration_check_passed", "rls_check_passed",
            "transport_test_passed", "reconcile_test_passed",
            "performance_baseline_id", "certification_id",
            "failure_reason", "rehearsal_log",
        ),
        indexes=("pg_rehearsal_status_idx", "pg_rehearsal_version_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="sqlite_runtime_compat",
        columns=(
            "compat_id", "python_version", "sqlite_library_version",
            "sqlite_source", "fts5_available", "wal_mode_available",
            "json1_available", "tested_at", "tested_by",
            "test_result", "notes",
        ),
        indexes=("sqlite_rt_compat_py_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="qdrant_contract_compat",
        columns=(
            "compat_id", "from_version", "to_version", "check_category",
            "passed", "tested_at", "tested_by", "test_details",
            "failure_reason", "migration_notes",
        ),
        indexes=("qdrant_compat_version_idx", "qdrant_compat_passed_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="sbom_dependency_inventory",
        columns=(
            "sbom_id", "release_id", "component", "component_type",
            "version", "source", "source_hash", "install_path",
            "license_name", "verified", "verified_at", "verified_by",
            "notes", "created_at",
        ),
        indexes=("sbom_release_idx", "sbom_component_idx", "sbom_verified_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="vulnerability_risk",
        columns=(
            "vuln_id", "component", "affected_versions", "fixed_version",
            "cve_id", "risk_level", "recommended_action",
            "upgrade_deadline_days", "description", "detected_at",
            "resolved_at", "resolved_by", "resolution_notes",
        ),
        indexes=("vuln_risk_component_idx", "vuln_risk_unresolved_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="dependency_drift",
        columns=(
            "drift_id", "release_id", "component", "expected_version",
            "installed_version", "drift_status", "detected_at",
            "detected_by", "resolved_at", "resolved_by", "resolution",
        ),
        indexes=("dep_drift_status_idx", "dep_drift_component_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="offline_bundle",
        columns=(
            "bundle_id", "release_id", "component", "version",
            "package_type", "platform", "storage_locator", "file_hash",
            "file_size_bytes", "hash_algorithm", "verified",
            "verified_at", "verified_by", "created_at", "notes",
        ),
        indexes=("offline_bundle_release_idx", "offline_bundle_component_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="release_signature",
        columns=(
            "signature_id", "release_id", "bundle_hash", "component_count",
            "component_hashes", "signature_algorithm", "signed_by",
            "signed_at", "verified_at", "verified_by",
            "verification_result", "tamper_state",
        ),
        indexes=("release_sig_release_idx", "release_sig_tamper_idx"),
    ),
    # Phase J: recovery orchestration
    TableContract(
        schema="gptbridge_index",
        table="recovery_plan",
        columns=(
            "plan_id", "version", "incident_type", "full_plan_id",
            "preconditions", "steps", "timeouts", "rollback_strategy",
            "verification_rules", "required_authority", "status",
            "created_at", "certified_at", "certified_by", "updated_at",
        ),
        indexes=("recovery_plan_incident_idx", "recovery_plan_status_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_incident",
        columns=(
            "incident_id", "plan_id", "incident_type", "detected_at",
            "detected_by", "description", "affected_components",
            "severity", "status", "recovery_generation",
            "degraded_generation", "resolved_generation",
            "resolved_at", "resolved_by", "resolution_notes",
            "incident_log", "updated_at",
        ),
        indexes=("recovery_incident_status_idx", "recovery_incident_type_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_state_machine",
        columns=(
            "state_id", "incident_id", "from_state", "to_state",
            "transition_at", "transitioned_by", "reason",
        ),
        indexes=("recovery_state_incident_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_state_transition",
        columns=("from_state", "to_state", "allowed", "requires_authority"),
        indexes=(),
    ),
    TableContract(
        schema="gptbridge_index",
        table="pg_offline_recovery",
        columns=(
            "recovery_id", "incident_id", "failure_detected_at",
            "confirmed_at", "confirmation_attempts", "degraded_at",
            "recovered_at", "degraded_generation", "recovered_generation",
            "modules_on_fallback", "fallback_status", "notes",
        ),
        indexes=("pg_offline_status_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="pg_recovery_verification",
        columns=(
            "verification_id", "incident_id", "verified_at", "verified_by",
            "connection_ok", "schema_version_ok", "database_release_ok",
            "roles_ok", "rls_ok", "audit_ok", "transport_ok",
            "integrity_ok", "generation_ok", "overall_recoverable",
            "failure_reason",
        ),
        indexes=("pg_recovery_verify_incident_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="reconcile_recovery_phase",
        columns=(
            "phase_id", "incident_id", "started_at", "completed_at",
            "status", "pending_snapshot_count", "reconciled_count",
            "conflict_count", "verified_count", "last_processed_revision",
            "batch_cursor", "conflict_details", "failure_reason",
        ),
        indexes=("reconcile_recovery_incident_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_generation",
        columns=(
            "generation_id", "generation_number", "incident_id",
            "generation_type", "previous_generation", "started_at",
            "ended_at", "description",
        ),
        indexes=("recovery_gen_number_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_barrier",
        columns=(
            "barrier_id", "incident_id", "barrier_type", "raised_at",
            "raised_by", "reason", "critical_reconcile_complete",
            "authority_conflicts_resolved", "integrity_pass",
            "barrier_active", "released_at", "released_by", "release_reason",
        ),
        indexes=("recovery_barrier_active_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="transport_recovery",
        columns=(
            "recovery_id", "incident_id", "request_id", "idempotency_key",
            "operation_id", "resource_revision", "commit_state",
            "detected_at", "resolved_at", "resolved_state",
            "resolution_method", "resolution_notes",
        ),
        indexes=("transport_recovery_idempotency_idx", "transport_recovery_unknown_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="unknown_commit_resolution",
        columns=(
            "resolution_id", "transport_recovery_id", "idempotency_key",
            "operation_id", "resource_revision", "lookup_method",
            "lookup_result", "found_committed", "resolved_state",
            "resolved_at", "resolved_by", "notes",
        ),
        indexes=("unknown_commit_res_key_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="lease_recovery",
        columns=(
            "lease_recovery_id", "incident_id", "request_id",
            "original_worker", "original_claimed_at", "lease_until",
            "worker_generation", "lease_status", "checked_at",
            "reclaimed_at", "reclaimed_by", "attempt_history",
        ),
        indexes=("lease_recovery_status_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="sqlite_fallback_freeze",
        columns=(
            "freeze_id", "incident_id", "module_id", "fallback_state",
            "entered_at", "transitioned_at", "transitioned_by",
            "pending_count", "drained_count", "notes",
        ),
        indexes=("sqlite_fallback_state_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_priority",
        columns=(
            "priority_id", "priority_class", "priority_level",
            "description", "max_parallel", "timeout_seconds", "created_at",
        ),
        indexes=(),
    ),
    TableContract(
        schema="gptbridge_index",
        table="qdrant_recovery",
        columns=(
            "recovery_id", "incident_id", "detected_at", "detected_by",
            "qdrant_status", "indexing_backlog_count",
            "missing_points_count", "stale_points_count",
            "rebuilt_points_count", "verified_points_count",
            "recovered_at", "notes",
        ),
        indexes=("qdrant_recovery_status_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="qdrant_full_rebuild",
        columns=(
            "rebuild_id", "incident_id", "old_collection_name",
            "new_collection_name", "collection_generation", "status",
            "total_chunks", "processed_chunks", "verified_chunks",
            "started_at", "completed_at", "switched_at",
            "old_collection_retired", "failure_reason",
        ),
        indexes=("qdrant_rebuild_status_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="sqlite_single_recovery",
        columns=(
            "recovery_id", "incident_id", "module_id", "database_path",
            "database_class", "failure_type", "recovery_action",
            "started_at", "completed_at", "status",
            "restored_from_backup_id", "new_generation", "notes",
        ),
        indexes=("sqlite_single_rec_module_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="codex_sqlite_recovery",
        columns=(
            "recovery_id", "incident_id", "codex_db_path", "failure_type",
            "status", "known_hash", "actual_hash", "hash_verified",
            "restored_from_source", "restored_at", "verified_at",
            "resumed_at", "started_at", "notes",
        ),
        indexes=("codex_sqlite_rec_status_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="backup_restore_orchestration",
        columns=(
            "orchestration_id", "incident_id", "backup_id", "backup_hash",
            "hash_verified", "temp_instance_name", "status",
            "started_at", "completed_at", "schema_verified",
            "release_verified", "rls_verified", "audit_verified",
            "integrity_verified", "promoted", "post_backup_reconciled",
            "failure_reason",
        ),
        indexes=("backup_restore_status_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="pitr_boundary",
        columns=(
            "pitr_id", "incident_id", "restore_target",
            "database_generation", "audit_head_hash",
            "transport_cutoff", "reconcile_cutoff",
            "pg_state_at_target", "sqlite_state_ahead",
            "qdrant_state_ahead", "cross_engine_reconcile_done",
            "cross_engine_reconcile_started_at",
            "cross_engine_reconcile_completed_at",
            "started_at", "notes",
        ),
        indexes=("pitr_boundary_incident_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_retry_policy",
        columns=(
            "policy_id", "plan_id", "step_name", "max_attempts",
            "timeout_seconds", "backoff_strategy",
            "initial_delay_seconds", "max_delay_seconds",
            "escalation_action", "created_at",
        ),
        indexes=("recovery_retry_plan_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_checkpoint",
        columns=(
            "checkpoint_id", "recovery_run_id", "incident_id",
            "current_phase", "last_completed_step",
            "last_processed_revision", "batch_cursor", "generation",
            "total_processed", "total_failed", "saved_at",
            "resumed_at", "resumed_count", "status",
        ),
        indexes=("recovery_checkpoint_run_idx", "recovery_checkpoint_active_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_idempotency",
        columns=(
            "idempotency_id", "recovery_run_id", "operation_key",
            "operation_type", "completed", "completed_at",
            "result", "attempts", "created_at",
        ),
        indexes=("recovery_idempotency_run_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_safety_fence",
        columns=(
            "fence_id", "forbidden_action", "description",
            "requires_authority", "blocked_count", "last_blocked_at",
            "created_at",
        ),
        indexes=(),
    ),
    TableContract(
        schema="gptbridge_index",
        table="chaos_drill",
        columns=(
            "drill_id", "scenario_code", "scenario_name", "description",
            "plan_id", "started_at", "completed_at", "status",
            "no_authority_inversion", "no_duplicate_write",
            "no_lost_commit", "no_silent_conflict",
            "no_uncontrolled_retry", "overall_passed",
            "failure_reason", "drill_log",
        ),
        indexes=("chaos_drill_status_idx", "chaos_drill_scenario_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="recovery_certification",
        columns=(
            "certification_id", "plan_id", "plan_version",
            "test_scenario", "database_release_id",
            "starting_generation", "ending_generation",
            "rpo_result", "rto_result", "rpo_seconds", "rto_seconds",
            "integrity_result", "reconcile_result", "audit_result",
            "certification_status", "certified_at", "certified_by",
            "expires_at", "notes", "created_at",
        ),
        indexes=("recovery_cert_plan_idx", "recovery_cert_status_idx"),
    ),
    # Phase K: data layer contract
    TableContract(
        schema="gptbridge_index",
        table="data_layer_contract",
        columns=(
            "contract_id", "contract_version", "database_release_id",
            "postgresql_schema_version", "sqlite_template_version",
            "qdrant_contract_version", "security_generation",
            "data_generation", "required_capabilities",
            "optional_capabilities", "startup_order", "shutdown_order",
            "degradation_policy", "recovery_policy", "status",
            "created_at", "activated_at", "updated_at",
        ),
        indexes=("data_layer_contract_status_idx", "data_layer_contract_version_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="dependency_classification",
        columns=(
            "classification_id", "component_name", "dependency_type",
            "component_category", "failure_effect", "criticality",
            "fallback_component", "fallback_boundary", "description",
            "created_at", "updated_at",
        ),
        indexes=("dep_class_type_idx", "dep_class_category_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="startup_phase",
        columns=(
            "phase_id", "phase_number", "phase_name", "description",
            "required_components", "write_enabled", "can_accept_requests",
            "created_at",
        ),
        indexes=(),
    ),
    TableContract(
        schema="gptbridge_index",
        table="startup_phase_gate",
        columns=(
            "gate_id", "phase_number", "gate_name", "gate_type",
            "check_expression", "required_for_write",
            "required_for_requests", "passed", "checked_at",
            "failure_reason", "created_at",
        ),
        indexes=("startup_gate_phase_idx", "startup_gate_write_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="schema_readiness",
        columns=(
            "readiness_id", "schema_name", "ready", "checked_at",
            "schema_version", "migration_head", "rls_enabled",
            "force_rls", "required_roles_present",
            "public_grants_revoked", "security_generation",
            "failure_reason", "updated_at",
        ),
        indexes=("schema_readiness_ready_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="rag_readiness_gate",
        columns=(
            "gate_id", "checked_at", "pg_rag_metadata_ready",
            "qdrant_ready", "metadata_authority_wired",
            "collection_contract_valid", "rag_ready", "failure_reason",
        ),
        indexes=("rag_gate_ready_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="shutdown_phase",
        columns=(
            "phase_id", "phase_number", "phase_name", "description",
            "actions", "timeout_seconds", "created_at",
        ),
        indexes=(),
    ),
    TableContract(
        schema="gptbridge_index",
        table="shutdown_audit",
        columns=(
            "shutdown_id", "started_at", "completed_at",
            "shutdown_status", "drain_result", "pending_operations",
            "pending_transport", "reconcile_pending",
            "database_generation", "security_generation",
            "active_leases_released", "active_leases_expired",
            "sqlite_checkpoints_done", "qdrant_cursor_saved",
            "audit_flushed", "notes",
        ),
        indexes=("shutdown_audit_status_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="unclean_shutdown_detection",
        columns=(
            "detection_id", "detected_at", "last_shutdown_id",
            "was_graceful", "extra_recovery_steps", "steps_completed",
            "all_steps_done", "completed_at",
        ),
        indexes=("unclean_detect_at_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="cache_invalidation_policy",
        columns=(
            "policy_id", "cache_name", "check_generation_compatible",
            "check_revision_compatible", "check_ttl_valid",
            "ttl_seconds", "on_mismatch", "created_at",
        ),
        indexes=("cache_inval_name_idx",),
    ),
    TableContract(
        schema="gptbridge_index",
        table="data_layer_dependency_graph",
        columns=(
            "edge_id", "from_component", "to_component",
            "edge_type", "boundary", "description", "created_at",
        ),
        indexes=("dep_graph_from_idx", "dep_graph_to_idx"),
    ),
    TableContract(
        schema="gptbridge_index",
        table="integration_rule",
        columns=(
            "rule_id", "rule_number", "rule_text", "rule_category",
            "enforced_by", "violation_effect", "active", "created_at",
        ),
        indexes=("integration_rule_number_idx",),
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

EXPECTED_MIGRATION_COUNT = 136  # every physical *.sql, including the 087/088 dual-numbered files


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
            rows = connection.execute(  # sql-ok: per-table contract verification, bounded by declared contract
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
            row = connection.execute(  # sql-ok: per-table RLS verification, bounded by declared contract
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
