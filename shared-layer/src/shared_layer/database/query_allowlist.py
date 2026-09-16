"""PostgreSQL Query Allowlist (A10/E10 + A8/E21).

Runtime code routes all SQL through fixed query templates registered here.
Modules must not submit free-form SQL to PostgreSQL.  Each template is
identified by a stable key; the actual SQL is stored centrally so it can
be audited, parameterized, and version-controlled.

Usage:
    from shared_layer.database.query_allowlist import QUERY_TEMPLATES, get_query

    sql = get_query("resource.get_by_id")
    connection.execute(sql, (resource_id,))

The allowlist is a frozen dict — it cannot be mutated at runtime.  New
queries must be added here and reviewed before deployment.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Mapping


# ============================================================================
# Query templates — each key maps to a parameterized SQL string.
# Parameters use %s (psycopg) or named :name (psycopg named).
# ============================================================================

_TEMPLATES: dict[str, str] = {
    # --- Resource (gptbridge_index.resource) ---
    "resource.get_by_id": (
        "SELECT resource_id, platform_id, module_id, owner_id, data_category, "
        "resource_type, resource_label, classification, locator_id, content_hash, "
        "version, index_status, metadata, created_at, updated_at, "
        "backend_generation, stale, authority_class, "
        "executor_id, correlation_id, source_revision "
        "FROM gptbridge_index.resource WHERE resource_id = %s"
    ),
    "resource.get_by_module": (
        "SELECT resource_id, version, content_hash, status, updated_at, "
        "authority_class "
        "FROM gptbridge_index.resource WHERE module_id = %s "
        "ORDER BY updated_at DESC LIMIT %s"
    ),
    "resource.set_status": (
        "UPDATE gptbridge_index.resource SET index_status = %s, updated_at = now() "
        "WHERE resource_id = %s"
    ),
    "resource.insert": (
        "INSERT INTO gptbridge_index.resource "
        "(resource_id, platform_id, module_id, owner_id, data_category, "
        "resource_type, resource_label, classification, locator_id, content_hash, "
        "version, index_status, metadata, authority_class) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    ),
    "resource.upsert": (
        "INSERT INTO gptbridge_index.resource "
        "(resource_id, platform_id, module_id, owner_id, data_category, "
        "resource_type, resource_label, classification, locator_id, content_hash, "
        "version, index_status, metadata, authority_class) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (resource_id) DO UPDATE SET "
        "version = excluded.version, content_hash = excluded.content_hash, "
        "index_status = excluded.index_status, metadata = excluded.metadata, "
        "authority_class = excluded.authority_class, updated_at = now()"
    ),
    "resource.count_by_module": (
        "SELECT count(*) FROM gptbridge_index.resource WHERE module_id = %s"
    ),
    "resource.consistency_view": (
        "SELECT resource_id, module_id, pg_revision, pg_hash, backend_generation, "
        "pg_stale, qdrant_revision, qdrant_hash, qdrant_generation, consistency_status "
        "FROM gptbridge_index.resource_consistency WHERE module_id = %s"
    ),

    # --- Data lineage (migration 018) ---
    "lineage.get_by_resource": (
        "SELECT resource_id, source_module, source_revision, produce_method, "
        "sync_path, last_writer_id, last_writer_at, "
        "last_writer_actor_id, last_writer_executor_id, "
        "last_writer_decision_id, last_writer_correlation_id, lineage_metadata "
        "FROM gptbridge_index.data_lineage WHERE resource_id = %s"
    ),
    "lineage.get_by_correlation": (
        "SELECT resource_id, source_module, source_revision, last_writer_at "
        "FROM gptbridge_index.data_lineage "
        "WHERE last_writer_correlation_id = %s ORDER BY last_writer_at"
    ),
    "lineage.resource_lineage_view": (
        "SELECT resource_id, module_id, authority_class, pg_revision, pg_hash, "
        "source_module, source_revision, produce_method, sync_path, "
        "last_writer_id, last_writer_at, last_writer_actor_id, "
        "last_writer_executor_id, last_writer_decision_id, "
        "last_writer_correlation_id, locator_id, locator_status, "
        "qdrant_authority_class, qdrant_revision, qdrant_hash, "
        "qdrant_index_status, qdrant_consistency "
        "FROM gptbridge_index.resource_lineage WHERE resource_id = %s"
    ),

    # --- RAG chunk (gptbridge_rag.chunk) ---
    "rag.chunk.get_by_resource": (
        "SELECT chunk_id, resource_id, module_id, sequence, qdrant_point_id, "
        "embedding_model, locator_fragment, metadata "
        "FROM gptbridge_rag.chunk WHERE resource_id = %s ORDER BY sequence"
    ),
    "rag.chunk.insert": (
        "INSERT INTO gptbridge_rag.chunk "
        "(chunk_id, resource_id, module_id, sequence, character_start, character_end, "
        "qdrant_point_id, embedding_model, locator_fragment, metadata) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
    ),
    "rag.chunk.delete_by_resource": (
        "DELETE FROM gptbridge_rag.chunk WHERE resource_id = %s"
    ),
    "rag.chunk.fts_search": (
        "SELECT chunk_id, resource_id, module_id, ts_rank_cd(content_tsv, query) AS rank "
        "FROM gptbridge_rag.chunk, plainto_tsquery('simple', %s) AS query "
        "WHERE content_tsv @@ query AND module_id = %s "
        "ORDER BY rank DESC LIMIT %s"
    ),

    # --- RAG index_state (gptbridge_rag.index_state) ---
    "rag.index_state.get": (
        "SELECT resource_id, module_id, embedding_model, qdrant_collection, "
        "chunk_count, status, version, indexed_at, updated_at, "
        "qdrant_point_id, source_revision, content_hash, backend_generation "
        "FROM gptbridge_rag.index_state WHERE resource_id = %s"
    ),
    "rag.index_state.upsert": (
        "INSERT INTO gptbridge_rag.index_state "
        "(resource_id, module_id, embedding_model, qdrant_collection, chunk_count, "
        "status, version, qdrant_point_id, source_revision, content_hash, "
        "backend_generation, authority_class) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (resource_id) DO UPDATE SET "
        "chunk_count = excluded.chunk_count, status = excluded.status, "
        "version = excluded.version, qdrant_point_id = excluded.qdrant_point_id, "
        "source_revision = excluded.source_revision, content_hash = excluded.content_hash, "
        "authority_class = excluded.authority_class, updated_at = now()"
    ),

    # --- Transport (gptbridge_transport.tool_request) ---
    "transport.submit": (
        "INSERT INTO gptbridge_transport.tool_request "
        "(channel_id, request_id, requester_actor, target_tool_id, payload, status, "
        "priority_class, priority_value, deadline_at, created_at, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, 'queued', %s, "
        "gptbridge_transport.priority_value_for(%s), %s, now(), now())"
    ),
    "transport.claim": (
        "SELECT request_id, requester_actor, payload "
        "FROM gptbridge_transport.tool_request "
        "WHERE channel_id = %s AND target_tool_id = %s AND status = 'queued' "
        "AND (next_retry_at IS NULL OR next_retry_at <= now()) "
        "AND (deadline_at IS NULL OR deadline_at > now()) "
        "ORDER BY priority_value, created_at, request_id LIMIT 1 FOR UPDATE SKIP LOCKED"
    ),
    "transport.claim_update": (
        "UPDATE gptbridge_transport.tool_request "
        "SET status = 'claimed', claimed_at = now(), "
        "lease_until = now() + (%s || ' seconds')::interval, "
        "attempt_count = attempt_count + 1, next_retry_at = NULL, updated_at = now() "
        "WHERE channel_id = %s AND request_id = %s AND status = 'queued'"
    ),
    "transport.respond": (
        "UPDATE gptbridge_transport.tool_request "
        "SET status = 'completed', response = %s, updated_at = now() "
        "WHERE channel_id = %s AND request_id = %s AND target_tool_id = %s AND status = 'claimed'"
    ),
    "transport.cancel": (
        "UPDATE gptbridge_transport.tool_request "
        "SET status = 'cancelled', updated_at = now() "
        "WHERE channel_id = %s AND request_id = %s AND target_tool_id = %s "
        "AND requester_actor = %s AND status IN ('queued', 'claimed')"
    ),
    "transport.reclaim_expired": (
        "UPDATE gptbridge_transport.tool_request "
        "SET status = 'queued', claimed_at = NULL, lease_until = NULL, "
        "next_retry_at = now(), updated_at = now() "
        "WHERE channel_id = %s AND target_tool_id = %s AND status = 'claimed' "
        "AND lease_until IS NOT NULL AND lease_until < now()"
    ),
    "transport.move_to_dead_letter": (
        "UPDATE gptbridge_transport.tool_request "
        "SET status = 'dead-letter', dead_letter_reason = %s, dead_letter_at = now(), "
        "updated_at = now() "
        "WHERE channel_id = %s AND request_id = %s "
        "AND status IN ('queued', 'claimed') AND attempt_count >= %s"
    ),
    "transport.get_dead_letter": (
        "SELECT request_id, target_tool_id, payload, dead_letter_reason, "
        "dead_letter_at, attempt_count "
        "FROM gptbridge_transport.tool_request "
        "WHERE channel_id = %s AND status = 'dead-letter' "
        "ORDER BY dead_letter_at DESC LIMIT %s"
    ),

    # --- Audit (gptbridge_audit.event) ---
    "audit.insert": (
        "INSERT INTO gptbridge_audit.event "
        "(event_id, actor_id, module_id, resource_id, action, outcome, decision_id, details) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    ),
    "audit.get_head": (
        "SELECT event_id, module_id, sequence_number, occurred_at "
        "FROM gptbridge_audit.event ORDER BY occurred_at DESC LIMIT 1"
    ),
    "audit.count_by_module": (
        "SELECT count(*) FROM gptbridge_audit.event WHERE module_id = %s"
    ),

    # --- Reconcile conflict log ---
    "reconcile_conflict.insert": (
        "INSERT INTO gptbridge_index.reconcile_conflict_log "
        "(module_id, resource_id, conflict_type, local_version, central_version, "
        "local_hash, central_hash, detail) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
    ),
    "reconcile_conflict.list_pending": (
        "SELECT conflict_id, module_id, resource_id, conflict_type, "
        "local_version, central_version, detail, detected_at "
        "FROM gptbridge_index.reconcile_conflict_log "
        "WHERE resolution_action = 'pending' ORDER BY detected_at"
    ),

    # --- Backup catalog ---
    "backup_catalog.insert": (
        "INSERT INTO gptbridge_index.backup_catalog "
        "(engine, source_path, backup_path, source_generation, schema_version, "
        "backup_hash, size_bytes) VALUES (%s, %s, %s, %s, %s, %s, %s)"
    ),
    "backup_catalog.mark_certified": (
        "UPDATE gptbridge_index.backup_catalog "
        "SET restore_tested_at = now(), restore_certified = true, "
        "restore_certification = %s WHERE backup_id = %s"
    ),
    "backup_catalog.list_uncertified": (
        "SELECT backup_id, engine, backup_path, created_at "
        "FROM gptbridge_index.backup_catalog WHERE restore_certified = false "
        "ORDER BY created_at"
    ),

    # --- Generation fence ---
    "generation.current": (
        "SELECT COALESCE(MAX(generation), 1) FROM gptbridge_index.backend_generation_state"
    ),
    "generation.bump": (
        "INSERT INTO gptbridge_index.backend_generation_state (generation, reason, set_by) "
        "VALUES (%s, %s, %s)"
    ),

    # --- Maintenance window ---
    "maintenance.active": (
        "SELECT 1 FROM gptbridge_index.maintenance_window "
        "WHERE status = 'running' AND scheduled_start <= now() LIMIT 1"
    ),
    "maintenance.scheduled_soon": (
        "SELECT 1 FROM gptbridge_index.maintenance_window "
        "WHERE status = 'scheduled' AND scheduled_start <= now() + (%s || ' minutes')::interval "
        "AND scheduled_start >= now() LIMIT 1"
    ),

    # --- Retention ---
    "retention.list_policies": (
        "SELECT schema_name, table_name, retention_days, retention_column, purge_method, enabled "
        "FROM gptbridge_index.retention_policy WHERE enabled = true"
    ),

    # --- Schema version ---
    "schema_version.count": (
        "SELECT count(*) FROM gptbridge_migration.history"
    ),
    "schema_version.list": (
        "SELECT migration_id, checksum, applied_at "
        "FROM gptbridge_migration.history ORDER BY migration_id"
    ),

    # --- Contract version (migration 024) ---
    "contract_version.get": (
        "SELECT contract_name, current_version, min_compatible_version, description "
        "FROM gptbridge_index.contract_version WHERE contract_name = %s"
    ),
    "contract_version.list": (
        "SELECT contract_name, current_version, min_compatible_version "
        "FROM gptbridge_index.contract_version ORDER BY contract_name"
    ),
    "contract_version.check_compatible": (
        "SELECT gptbridge_index.check_contract_compatibility(%s, %s)"
    ),

    # --- Permission snapshot (migration 021) ---
    "permission_snapshot.get_by_event": (
        "SELECT snapshot_id, actor_id, session_user, target_module, "
        "target_resource_id, target_classification, evaluated_roles, "
        "evaluated_policies, decision_summary, rls_context, "
        "can_read, can_write, can_write_resource, captured_at "
        "FROM gptbridge_audit.permission_snapshot WHERE event_id = %s"
    ),
    "permission_snapshot.get_by_actor": (
        "SELECT snapshot_id, target_module, target_resource_id, "
        "can_write, can_write_resource, captured_at "
        "FROM gptbridge_audit.permission_snapshot "
        "WHERE actor_id = %s ORDER BY captured_at DESC LIMIT %s"
    ),

    # --- DDL audit (migration 023) ---
    "ddl_audit.recent": (
        "SELECT ddl_event_id, command_tag, object_identity, schema_name, "
        "object_name, session_user, migration_executor, occurred_at "
        "FROM gptbridge_audit.ddl_event ORDER BY occurred_at DESC LIMIT %s"
    ),
    "ddl_audit.by_schema": (
        "SELECT ddl_event_id, command_tag, object_name, session_user, occurred_at "
        "FROM gptbridge_audit.ddl_event WHERE schema_name = %s "
        "ORDER BY occurred_at DESC LIMIT %s"
    ),

    # --- Workload class (migration 026) ---
    "workload_class.get": (
        "SELECT class_name, pool_owner, statement_timeout_ms, lock_timeout_ms, "
        "priority, description "
        "FROM gptbridge_index.workload_class WHERE class_name = %s"
    ),
    "workload_class.list": (
        "SELECT class_name, pool_owner, statement_timeout_ms, lock_timeout_ms, priority "
        "FROM gptbridge_index.workload_class ORDER BY class_name"
    ),

    # --- Generation fence (migration 016/025) ---
    "generation.sqlite_stale": (
        "SELECT module_id, database_path, backend_generation, last_synced_at "
        "FROM gptbridge_index.sqlite_generation WHERE stale = true "
        "ORDER BY updated_at"
    ),

    # --- Two-stage deletion (migration 027) ---
    "deletion.tombstone": (
        "SELECT gptbridge_index.tombstone_resource(%s, %s)"
    ),
    "deletion.advance": (
        "SELECT gptbridge_index.advance_deletion_stage(%s)"
    ),
    "deletion.purge_eligible": (
        "SELECT resource_id, module_id, tombstoned_at, purge_after "
        "FROM gptbridge_index.resource "
        "WHERE deletion_stage = 'tombstone' AND purge_after IS NOT NULL "
        "AND now() >= purge_after ORDER BY purge_after LIMIT %s"
    ),
    "deletion.by_stage": (
        "SELECT resource_id, module_id, deletion_stage, tombstoned_at, purge_after "
        "FROM gptbridge_index.resource WHERE deletion_stage = %s "
        "ORDER BY tombstoned_at LIMIT %s"
    ),

    # --- Rebuild certification (migration 028) ---
    "rebuild_cert.latest": (
        "SELECT certification_id, engine, target, rebuild_reason, "
        "certified, certified_at, certified_by "
        "FROM gptbridge_index.rebuild_certification "
        "ORDER BY certified_at DESC LIMIT %s"
    ),
    "rebuild_cert.by_engine": (
        "SELECT certification_id, target, certified, certified_at "
        "FROM gptbridge_index.rebuild_certification "
        "WHERE engine = %s ORDER BY certified_at DESC LIMIT %s"
    ),

    # --- Watchdog / bloat / RPO-RTO / capacity (migration 029) ---
    "watchdog.long_tx": (
        "SELECT pid, session_user, state, transaction_age_seconds, "
        "idle_in_transaction_seconds, lock_holder, detected_at "
        "FROM gptbridge_index.long_transaction_watchdog "
        "ORDER BY detected_at DESC LIMIT %s"
    ),
    "bloat.latest": (
        "SELECT schema_name, table_name, dead_tuples, live_tuples, "
        "table_size_bytes, collected_at "
        "FROM gptbridge_index.bloat_report "
        "ORDER BY collected_at DESC LIMIT %s"
    ),
    "rpo_rto.list": (
        "SELECT engine, rpo_seconds, rto_seconds, backup_frequency_seconds "
        "FROM gptbridge_index.rpo_rto_class ORDER BY engine"
    ),
    "capacity.list": (
        "SELECT metric_name, warning_level, critical_level, fail_closed_level, unit "
        "FROM gptbridge_index.capacity_threshold ORDER BY metric_name"
    ),

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

    # --- SQLite classification (migration 037) ---
    "sqlite_class.list": (
        "SELECT module_id, database_path, db_class, synchronous_setting, "
        "backup_frequency_seconds, integrity_check_frequency_seconds, "
        "retention_days, reconcile_required "
        "FROM gptbridge_index.sqlite_database_class ORDER BY module_id, database_path"
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
        "role_version, sqlite_template_version, qdrant_contract_version, "
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

    # --- SQLite template release (migration 045) ---
    "sqlite_template.active": (
        "SELECT template_version, schema_version, minimum_reader_version, "
        "minimum_writer_version, ddl_hash "
        "FROM gptbridge_index.sqlite_template_release "
        "WHERE status = 'active' ORDER BY template_version DESC LIMIT 1"
    ),

    # --- Qdrant contract (migration 046) ---
    "qdrant_contract.active": (
        "SELECT contract_version, collection_name, vector_dimension, "
        "distance_metric, embedding_model "
        "FROM gptbridge_index.qdrant_contract WHERE status = 'active' "
        "ORDER BY contract_version DESC LIMIT 1"
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

    # --- SQLite retention (migration 053) ---
    "sqlite_retention.by_class": (
        "SELECT db_class, retention_days, archive_eligible, archive_after_days, "
        "purge_eligible, purge_after_days, version_history_required "
        "FROM gptbridge_index.sqlite_retention_policy ORDER BY db_class"
    ),

    # --- Qdrant vector lifecycle (migration 054) ---
    "qdrant_vector.pending_deletion": (
        "SELECT resource_id, collection_name, point_id, pg_marked_at "
        "FROM gptbridge_index.get_vectors_pending_deletion(%s)"
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

    # --- SQLite database digest (migration 067) ---
    "sqlite_digest.list": (
        "SELECT digest_id, module_id, database_path, schema_hash, "
        "revision_head, row_count, generation, tamper_state, computed_at "
        "FROM gptbridge_index.sqlite_database_digest "
        "ORDER BY computed_at DESC LIMIT %s"
    ),
    "sqlite_digest.tampered": (
        "SELECT module_id, database_path, tamper_state, computed_at "
        "FROM gptbridge_index.get_tampered_sqlite_dbs(%s)"
    ),

    # --- Qdrant integrity mapping (migration 068) ---
    "qdrant_integrity.issues": (
        "SELECT chunk_id, resource_id, integrity_state, qdrant_point_id "
        "FROM gptbridge_index.get_qdrant_integrity_issues(%s)"
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

    # --- Fail-closed (migration 073) ---
    "fail_closed.active": (
        "SELECT action_id, trigger_type, action_taken, domain, triggered_at "
        "FROM gptbridge_index.get_active_fail_closed(%s)"
    ),

    # --- Version lock (migration 074) ---
    "version_lock.list": (
        "SELECT component, version_string, major_version, minor_version, "
        "patch_version, locked_at FROM gptbridge_index.version_lock "
        "WHERE release_id = %s ORDER BY component"
    ),

    # --- Compatibility matrix ext (migration 075) ---
    "compat_matrix_ext.list": (
        "SELECT postgresql_version, psycopg_version, "
        "sqlite_runtime_version, qdrant_server_version, status, tested_at "
        "FROM gptbridge_index.compatibility_matrix_ext "
        "WHERE release_id = %s ORDER BY updated_at DESC"
    ),
    "compat_matrix_ext.forbidden": (
        "SELECT postgresql_version, psycopg_version, "
        "sqlite_runtime_version, qdrant_server_version, notes "
        "FROM gptbridge_index.get_forbidden_combinations()"
    ),

    # --- Upgrade classification (migration 076) ---
    "upgrade_classification.list": (
        "SELECT component, from_version, to_version, upgrade_class, "
        "required_validation, allows_unattended, classified_at "
        "FROM gptbridge_index.upgrade_classification "
        "ORDER BY classified_at DESC LIMIT %s"
    ),

    # --- Driver compatibility test (migration 077) ---
    "driver_compat.failed": (
        "SELECT test_id, driver_name, driver_version, test_category, "
        "failure_reason, tested_at "
        "FROM gptbridge_index.get_failed_driver_tests(NULL, %s)"
    ),

    # --- PG major upgrade rehearsal (migration 078) ---
    "pg_rehearsal.list": (
        "SELECT rehearsal_id, from_version, to_version, status, "
        "started_at, completed_at "
        "FROM gptbridge_index.pg_major_upgrade_rehearsal "
        "ORDER BY started_at DESC LIMIT %s"
    ),

    # --- SQLite runtime compat (migration 079) ---
    "sqlite_runtime_compat.list": (
        "SELECT python_version, sqlite_library_version, "
        "fts5_available, wal_mode_available, json1_available, tested_at "
        "FROM gptbridge_index.sqlite_runtime_compat "
        "ORDER BY tested_at DESC LIMIT %s"
    ),

    # --- Qdrant contract compat (migration 080) ---
    "qdrant_compat.list": (
        "SELECT from_version, to_version, check_category, passed, "
        "tested_at FROM gptbridge_index.qdrant_contract_compat "
        "ORDER BY tested_at DESC LIMIT %s"
    ),

    # --- SBOM (migration 081) ---
    "sbom.list": (
        "SELECT component, component_type, version, source, source_hash, "
        "install_path, verified FROM gptbridge_index.sbom_dependency_inventory "
        "WHERE release_id = %s ORDER BY component_type, component"
    ),

    # --- Vulnerability risk (migration 082) ---
    "vulnerability.critical": (
        "SELECT vuln_id, component, affected_versions, fixed_version, "
        "cve_id, risk_level, recommended_action, upgrade_deadline_days "
        "FROM gptbridge_index.get_critical_vulnerabilities()"
    ),

    # --- Dependency drift (migration 083) ---
    "dependency_drift.unverified": (
        "SELECT drift_id, component, expected_version, installed_version, "
        "drift_status, detected_at "
        "FROM gptbridge_index.get_unverified_dependencies()"
    ),

    # --- Offline bundle (migration 084) ---
    "offline_bundle.list": (
        "SELECT bundle_id, component, version, package_type, platform, "
        "file_hash, verified FROM gptbridge_index.offline_bundle "
        "WHERE release_id = %s ORDER BY component, version"
    ),

    # --- Release signature (migration 085) ---
    "release_signature.latest": (
        "SELECT signature_id, bundle_hash, component_count, "
        "tamper_state, signed_at "
        "FROM gptbridge_index.get_latest_signature(%s)"
    ),
}

QUERY_TEMPLATES: Mapping[str, str] = MappingProxyType(_TEMPLATES)


def get_query(key: str) -> str:
    """Get a query template by key.  Raises KeyError if not registered."""
    if key not in QUERY_TEMPLATES:
        raise KeyError(
            f"QUERY_NOT_ALLOWLISTED: '{key}' is not in the query allowlist. "
            f"Add it to shared_layer.database.query_allowlist.QUERY_TEMPLATES."
        )
    return QUERY_TEMPLATES[key]


def is_allowlisted(key: str) -> bool:
    """Check if a query key is in the allowlist."""
    return key in QUERY_TEMPLATES


def validate_query(sql: str) -> tuple[bool, str | None]:
    """Validate that a raw SQL string matches an allowlisted template.

    Returns (is_valid, matched_key_or_reason).
    """
    normalized = " ".join(sql.split())
    for key, template in QUERY_TEMPLATES.items():
        if " ".join(template.split()) == normalized:
            return True, key
    return False, "SQL does not match any allowlisted template"


__all__ = [
    "QUERY_TEMPLATES",
    "get_query",
    "is_allowlisted",
    "validate_query",
]
