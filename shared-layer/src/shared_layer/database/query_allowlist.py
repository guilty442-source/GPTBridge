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
