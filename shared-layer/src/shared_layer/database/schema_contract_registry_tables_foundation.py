"""Declared foundation table contracts (security principal, index core, RAG, transport, audit base)."""
from __future__ import annotations

from .schema_contract_registry_types import TableContract


DECLARED_TABLES_FOUNDATION: tuple[TableContract, ...] = (
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
            "character_start", "character_end", "vector_point_id",
            "embedding_model", "locator_fragment", "metadata", "created_at",
            "content_tsv",
        ),
        indexes=("rag_chunk_module_idx", "rag_chunk_fts_idx", "rag_chunk_module_fts_idx"),
    ),
    TableContract(
        schema="gptbridge_rag",
        table="index_state",
        columns=(
            "resource_id", "module_id", "embedding_model", "vector_collection",
            "chunk_count", "status", "version", "indexed_at", "updated_at",
            "embedding_dimension", "chunk_size", "chunk_overlap", "content_hash",
            "vector_point_id", "postgresql_record_id", "source_revision",
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
)
