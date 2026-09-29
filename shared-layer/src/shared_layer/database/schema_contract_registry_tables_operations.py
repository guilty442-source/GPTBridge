"""Declared operations table contracts (Phase E observability, F release management, G lifecycle, H integrity)."""
from __future__ import annotations

from .schema_contract_registry_types import TableContract


DECLARED_TABLES_OPERATIONS: tuple[TableContract, ...] = (
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
            "rls_version", "role_version",
            "reconcile_contract_version", "vector_contract_version",
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
)
