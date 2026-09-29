"""Allowlisted query templates: operations domains (releases, retention, purge, integrity)."""
from __future__ import annotations


# ============================================================================
# Query templates — each key maps to a parameterized SQL string.
# Parameters use %s (psycopg) or named :name (psycopg named).
# ============================================================================

TEMPLATES_OPS: dict[str, str] = {
    # --- Read-only domain (migration 030) ---
    "readonly_domain.list": (
        "SELECT domain_name, is_readonly, reason, activated_at "
        "FROM gptbridge_index.readonly_domain ORDER BY domain_name"
    ),

    # --- Startup certification (migration 030) ---
    "startup_cert.latest": (
        "SELECT certification_id, ready, schema_version_verified, rls_verified, "
        "required_roles_verified, migration_head_verified, "
        "audit_append_only_verified, authority_contract_verified, "
        "contract_version_verified, certified_at "
        "FROM gptbridge_index.startup_certification "
        "ORDER BY certified_at DESC LIMIT 1"
    ),

    # --- SLO metrics (migration 031) ---
    "slo_metric.list": (
        "SELECT metric_name, target_value, target_direction, unit, window_seconds "
        "FROM gptbridge_index.slo_metric ORDER BY metric_name"
    ),
    "slo_observation.recent": (
        "SELECT metric_name, observed_value, met_target, observed_at "
        "FROM gptbridge_index.slo_observation "
        "ORDER BY observed_at DESC LIMIT %s"
    ),

    # --- Workload pool config (migration 032) ---
    "workload_pool.list": (
        "SELECT pool_name, max_connections, max_open, max_idle, "
        "wait_timeout_seconds, description "
        "FROM gptbridge_index.workload_pool_config ORDER BY pool_name"
    ),

    # --- Query class (migration 032) ---
    "query_class.list": (
        "SELECT class_name, pool_name, statement_timeout_ms, lock_timeout_ms, "
        "retry_limit, batch_size, priority, description "
        "FROM gptbridge_index.query_class ORDER BY class_name"
    ),

    # --- Query fingerprint (migration 033) ---
    "query_fingerprint.hot": (
        "SELECT query_key, execution_count, mean_latency_ms, p95_latency_ms, "
        "rows_returned_total, rows_scanned_total "
        "FROM gptbridge_index.query_fingerprint "
        "WHERE execution_count > 0 "
        "ORDER BY p95_latency_ms DESC LIMIT %s"
    ),

    # --- Transport archive (migration 034) ---
    "tool_request_history.recent": (
        "SELECT request_id, channel_id, target_tool_id, status, archived_at "
        "FROM gptbridge_transport.tool_request_history "
        "ORDER BY archived_at DESC LIMIT %s"
    ),

    # --- Audit archive (migration 035) ---
    "audit_event_history.recent": (
        "SELECT event_id, event_type, actor, occurred_at, archived_at "
        "FROM gptbridge_audit.event_history "
        "ORDER BY archived_at DESC LIMIT %s"
    ),

    # --- Partition threshold (migration 035) ---
    "partition_threshold.list": (
        "SELECT table_schema, table_name, row_count_threshold, "
        "size_mb_threshold, latency_ms_threshold, partition_enabled "
        "FROM gptbridge_index.partition_threshold ORDER BY table_schema, table_name"
    ),

    # --- WAL checkpoint (migration 036) ---
    "wal_checkpoint.recent": (
        "SELECT wal_size_bytes, checkpoint_count, checkpoint_duration_ms, "
        "wal_rate_mb_per_min, collected_at "
        "FROM gptbridge_index.wal_checkpoint_snapshot "
        "ORDER BY collected_at DESC LIMIT %s"
    ),

    # --- Reconcile pending queue (migration 038) ---
    "reconcile_queue.pending": (
        "SELECT resource_id, source_revision, enqueued_at, last_reconciled_revision "
        "FROM gptbridge_index.reconcile_pending_queue "
        "WHERE module_id = %s AND dirty = true "
        "ORDER BY enqueued_at LIMIT %s"
    ),

    # --- Performance baseline (migration 039) ---
    "performance_baseline.latest": (
        "SELECT operation_name, p50_latency_ms, p95_latency_ms, p99_latency_ms, "
        "sample_count, baseline_at "
        "FROM gptbridge_index.performance_baseline "
        "ORDER BY baseline_at DESC LIMIT %s"
    ),

    # --- Database release manifest (migration 040) ---
    "database_release.active": (
        "SELECT release_id, schema_version, migration_head, rls_version, "
        "role_version, vector_contract_version, "
        "query_contract_version, minimum_runtime_version "
        "FROM gptbridge_index.database_release WHERE state = 'ACTIVE' "
        "ORDER BY activated_at DESC LIMIT 1"
    ),
    "database_release.list": (
        "SELECT release_id, state, schema_version, migration_head, "
        "created_at, activated_at "
        "FROM gptbridge_index.database_release ORDER BY created_at DESC LIMIT %s"
    ),

    # --- Compatibility matrix (migration 041) ---
    "release_compatibility.list": (
        "SELECT release_id, runtime_version, mode, reason "
        "FROM gptbridge_index.release_compatibility "
        "ORDER BY release_id, runtime_version"
    ),

    # --- Migration classification (migration 042) ---
    "migration_classification.list": (
        "SELECT migration_id, migration_name, change_type "
        "FROM gptbridge_index.migration_classification ORDER BY migration_id"
    ),
    "migration_classification.breaking": (
        "SELECT migration_id, migration_name, rollback_plan, recovery_plan "
        "FROM gptbridge_index.migration_classification "
        "WHERE change_type = 'breaking' ORDER BY migration_id"
    ),

    # --- Query contract version (migration 043) ---
    "query_contract.active": (
        "SELECT contract_name, version, sql_template, description "
        "FROM gptbridge_index.query_contract WHERE status = 'active' "
        "ORDER BY contract_name, version DESC"
    ),

    # --- RLS/Role migration (migration 044) ---
    "rls_role_migration.list": (
        "SELECT migration_id, rls_role_version, change_type, target_object, "
        "applied_at, applied_by "
        "FROM gptbridge_index.rls_role_migration ORDER BY applied_at DESC LIMIT %s"
    ),

    # --- Canary upgrade (migration 047) ---
    "canary_upgrade.list": (
        "SELECT canary_id, release_id, status, started_at, completed_at, "
        "promoted_to_production "
        "FROM gptbridge_index.canary_upgrade ORDER BY started_at DESC LIMIT %s"
    ),

    # --- Release audit (migration 048) ---
    "release_audit.history": (
        "SELECT audit_id, executor, result, started_at, completed_at, "
        "schema_hash, failure_reason "
        "FROM gptbridge_index.database_release_audit "
        "WHERE release_id = %s ORDER BY audited_at DESC"
    ),

    # --- Roll forward (migration 049) ---
    "roll_forward.list": (
        "SELECT corrective_migration_id, fixes_migration_id, description, "
        "applied_at, verified "
        "FROM gptbridge_index.roll_forward_migration ORDER BY corrective_migration_id"
    ),

    # --- Lifecycle state (migration 050) ---
    "lifecycle_state.get": (
        "SELECT lifecycle_state, previous_state, reason, transitioned_at "
        "FROM gptbridge_index.lifecycle_state "
        "WHERE entity_type = %s AND entity_id = %s"
    ),
    "lifecycle_state.by_state": (
        "SELECT entity_id, transitioned_at, reason "
        "FROM gptbridge_index.lifecycle_state "
        "WHERE entity_type = %s AND lifecycle_state = %s "
        "ORDER BY transitioned_at LIMIT %s"
    ),

    # --- Transport retention (migration 051) ---
    "transport_retention.list": (
        "SELECT status, hot_retention_days, archive_after_days, can_purge, "
        "purge_after_days FROM gptbridge_index.transport_retention_policy "
        "ORDER BY status"
    ),
    "transport_retention.archive_eligible": (
        "SELECT request_id, channel_id, target_tool_id, status, updated_at "
        "FROM gptbridge_index.get_transport_archive_eligible(%s)"
    ),

    # --- Audit retention (migration 052) ---
    "audit_retention.layers": (
        "SELECT layer_name, retention_days, next_layer, compression_enabled "
        "FROM gptbridge_index.audit_retention_layer ORDER BY retention_days"
    ),

    # --- Purge queue (migration 055) ---
    "purge_queue.eligible": (
        "SELECT queue_id, resource_id, module_id, entity_type, retention_until "
        "FROM gptbridge_index.get_purge_eligible(%s)"
    ),
    "purge_queue.list": (
        "SELECT queue_id, resource_id, entity_type, purge_status, "
        "requested_at, retention_until "
        "FROM gptbridge_index.purge_queue ORDER BY requested_at DESC LIMIT %s"
    ),

    # --- Archive catalog (migration 056) ---
    "archive_catalog.list": (
        "SELECT archive_id, source_engine, source_table, record_count, "
        "created_at, verified_at "
        "FROM gptbridge_index.archive_catalog ORDER BY created_at DESC LIMIT %s"
    ),

    # --- Retention hold (migration 059) ---
    "retention_hold.active": (
        "SELECT hold_id, entity_type, entity_id, hold_reason, placed_at "
        "FROM gptbridge_index.retention_hold WHERE hold_active = true "
        "ORDER BY placed_at DESC LIMIT %s"
    ),

    # --- Dependency check (migration 060) ---
    "dependency_check.recent": (
        "SELECT check_id, resource_id, has_dependencies, can_purge, checked_at "
        "FROM gptbridge_index.dependency_check ORDER BY checked_at DESC LIMIT %s"
    ),

    # --- Archive restore test (migration 061) ---
    "archive_restore_test.recent": (
        "SELECT test_id, archive_id, overall_passed, tested_at, failure_reason "
        "FROM gptbridge_index.archive_restore_test ORDER BY tested_at DESC LIMIT %s"
    ),

    # --- Capacity quota (migration 062) ---
    "capacity_quota.list": (
        "SELECT domain_name, soft_limit_mb, hard_limit_mb, archive_threshold_mb, "
        "emergency_threshold_mb, current_size_mb "
        "FROM gptbridge_index.capacity_quota ORDER BY domain_name"
    ),

    # --- Purge audit (migration 063) ---
    "purge_audit.recent": (
        "SELECT purge_id, resource_id, actor, executor, deleted_from, "
        "deleted_at, verified "
        "FROM gptbridge_index.purge_audit_log ORDER BY deleted_at DESC LIMIT %s"
    ),

    # --- Audit hash chain (migration 064) ---
    "audit_hash_chain.verify": (
        "SELECT event_id, sequence, expected_hash, actual_hash, chain_intact "
        "FROM gptbridge_audit.verify_audit_chain(%s)"
    ),
    "audit_hash_chain.head": (
        "SELECT gptbridge_audit.get_audit_head_hash()"
    ),

    # --- Reconcile batch digest (migration 065) ---
    "reconcile_batch.list": (
        "SELECT reconcile_run_id, module_id, source_generation, "
        "first_revision, last_revision, record_count, batch_hash, "
        "result_hash, status, started_at "
        "FROM gptbridge_index.reconcile_batch_digest "
        "ORDER BY started_at DESC LIMIT %s"
    ),

    # --- Resource content hash (migration 066) ---
    "resource_content_hash.list": (
        "SELECT resource_id, resource_hash, metadata_hash, locator_hash, "
        "revision, tamper_state, computed_at "
        "FROM gptbridge_index.resource_content_hash "
        "ORDER BY computed_at DESC LIMIT %s"
    ),
    "resource_content_hash.tampered": (
        "SELECT resource_id, tamper_state, revision, computed_at "
        "FROM gptbridge_index.get_tampered_resources(%s)"
    ),

    # --- Merkle root (migration 069) ---
    "merkle_root.list": (
        "SELECT merkle_id, domain, domain_id, leaf_count, merkle_root, "
        "tamper_state, computed_at "
        "FROM gptbridge_index.merkle_root ORDER BY computed_at DESC LIMIT %s"
    ),

    # --- Integrity snapshot (migration 070) ---
    "integrity_snapshot.latest": (
        "SELECT snapshot_id, database_generation, schema_hash, "
        "audit_head_hash, resource_merkle_root, migration_head, "
        "tamper_state, created_at "
        "FROM gptbridge_index.get_latest_snapshot()"
    ),

    # --- Restore verification (migration 071) ---
    "restore_verification.recent": (
        "SELECT verification_id, target_database, overall_passed, "
        "failure_reason, restored_at "
        "FROM gptbridge_index.restore_verification "
        "ORDER BY restored_at DESC LIMIT %s"
    ),

    # --- Tamper state (migration 072) ---
    "tamper_state.active": (
        "SELECT state_id, entity_type, entity_id, tamper_state, detected_at "
        "FROM gptbridge_index.get_active_tamper_issues(%s)"
    ),

}
