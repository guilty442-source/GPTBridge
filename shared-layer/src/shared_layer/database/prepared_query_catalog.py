"""Prepared Query Catalog (E13).

Central catalog of high-frequency SQL queries.  All runtime uses
fixed query contracts — easier to optimize, audit, prevent SQL
injection, fingerprint, and version.

Usage:
    from shared_layer.database.prepared_query_catalog import CATALOG

    sql = CATALOG["lookup_resource"]

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist; deny-by-default.
"""
from __future__ import annotations

from typing import Any

# ============================================================================
# Prepared Query Catalog
#
# Each entry is a named, versioned SQL contract.  Runtime code uses
# these names instead of inline SQL.
# ============================================================================
CATALOG: dict[str, dict[str, Any]] = {
    "lookup_resource": {
        "sql": (
            "SELECT resource_id, module_id, category, status, metadata, "
            "authority_class, executor_id, correlation_id, source_revision "
            "FROM gptbridge_index.resource WHERE resource_id = %s"
        ),
        "version": 1,
        "description": "Lookup a single resource by ID",
    },
    "lookup_resource_by_module": {
        "sql": (
            "SELECT resource_id, category, status, metadata "
            "FROM gptbridge_index.resource WHERE module_id = %s "
            "ORDER BY updated_at DESC LIMIT %s"
        ),
        "version": 1,
        "description": "Lookup resources by module",
    },
    "claim_request": {
        "sql": (
            "SELECT request_id, channel_id, target_tool_id, status, "
            "payload, priority, created_at "
            "FROM gptbridge_transport.tool_request "
            "WHERE channel_id = %s AND target_tool_id = %s AND status = 'queued' "
            "AND (next_retry_at IS NULL OR next_retry_at <= now()) "
            "ORDER BY created_at, request_id LIMIT %s FOR UPDATE SKIP LOCKED"
        ),
        "version": 1,
        "description": "Claim the next queued transport request",
    },
    "append_audit": {
        "sql": (
            "INSERT INTO gptbridge_audit.event "
            "(event_type, actor, detail, occurred_at, metadata) "
            "VALUES (%s, %s, %s, now(), %s)"
        ),
        "version": 1,
        "description": "Append an audit event",
    },
    "update_index_state": {
        "sql": (
            "INSERT INTO gptbridge_index.index_state "
            "(resource_id, index_type, indexed_at, parser_version, "
            "rag_schema_version, pipeline_version, authority_class) "
            "VALUES (%s, %s, now(), %s, %s, %s, %s) "
            "ON CONFLICT (resource_id, index_type) DO UPDATE SET "
            "indexed_at = now(), parser_version = EXCLUDED.parser_version, "
            "rag_schema_version = EXCLUDED.rag_schema_version, "
            "pipeline_version = EXCLUDED.pipeline_version, "
            "authority_class = EXCLUDED.authority_class"
        ),
        "version": 1,
        "description": "Upsert index state for a resource",
    },
    "lookup_locator": {
        "sql": (
            "SELECT locator_type, locator_value, metadata "
            "FROM gptbridge_index.resource_locator "
            "WHERE resource_id = %s AND locator_type = %s"
        ),
        "version": 1,
        "description": "Lookup a resource locator",
    },
    "fetch_relationships": {
        "sql": (
            "SELECT relation_type, target_resource_id, metadata "
            "FROM gptbridge_index.resource_relation "
            "WHERE source_resource_id = %s "
            "ORDER BY relation_type, target_resource_id"
        ),
        "version": 1,
        "description": "Fetch all relationships for a resource",
    },
    "lookup_lineage": {
        "sql": (
            "SELECT source_module, source_revision, generation_method, "
            "sync_path, last_writer "
            "FROM gptbridge_index.data_lineage "
            "WHERE resource_id = %s"
        ),
        "version": 1,
        "description": "Lookup data lineage for a resource",
    },
    "check_authority_class": {
        "sql": (
            "SELECT authority_class FROM gptbridge_index.resource "
            "WHERE resource_id = %s"
        ),
        "version": 1,
        "description": "Check the authority class of a resource",
    },
    "insert_resource": {
        "sql": (
            "INSERT INTO gptbridge_index.resource "
            "(resource_id, module_id, category, status, metadata, "
            "authority_class, source_revision) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)"
        ),
        "version": 1,
        "description": "Insert a new resource",
    },
    "update_resource_status": {
        "sql": (
            "UPDATE gptbridge_index.resource SET status = %s, "
            "updated_at = now() WHERE resource_id = %s"
        ),
        "version": 1,
        "description": "Update a resource's status",
    },
}


def get_query(name: str) -> str:
    """Get the SQL for a named query contract."""
    entry = CATALOG.get(name)
    if entry is None:
        raise KeyError(f"Unknown prepared query: {name}")
    return entry["sql"]


def get_query_info(name: str) -> dict[str, Any]:
    """Get full info (sql, version, description) for a named query."""
    entry = CATALOG.get(name)
    if entry is None:
        raise KeyError(f"Unknown prepared query: {name}")
    return dict(entry)


def list_queries() -> list[str]:
    """List all registered query names."""
    return sorted(CATALOG.keys())


def catalog_version() -> int:
    """Get the max version across all queries (catalog version)."""
    return max(entry["version"] for entry in CATALOG.values())


__all__ = [
    "CATALOG",
    "get_query",
    "get_query_info",
    "list_queries",
    "catalog_version",
]
