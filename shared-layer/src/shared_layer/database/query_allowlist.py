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
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, 'queued', now(), now())"
    ),
    "transport.claim": (
        "SELECT request_id, requester_actor, payload "
        "FROM gptbridge_transport.tool_request "
        "WHERE channel_id = %s AND target_tool_id = %s AND status = 'queued' "
        "AND (next_retry_at IS NULL OR next_retry_at <= now()) "
        "ORDER BY created_at, request_id LIMIT 1 FOR UPDATE SKIP LOCKED"
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
