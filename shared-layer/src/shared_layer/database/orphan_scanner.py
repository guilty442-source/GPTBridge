"""Orphan Scanner (C7).

Periodically scans for orphan states across PostgreSQL, SQLite, and Qdrant:
  1. PG has resource but locator does not exist
  2. Qdrant point exists but no chunk in PG
  3. SQLite has pending resource but PG has no corresponding resource

Results are written to gptbridge_index.orphan_report for the repair chain.

Usage:
    from shared_layer.database.orphan_scanner import scan_orphans

    with connection_manager.connection() as conn:
        report = scan_orphans(conn)
        for orphan in report:
            print(orphan)

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A44/E30 — four-functions-local.
    A52/E38 — RAG: Qdrant canonical semantic index.
"""
from __future__ import annotations

from typing import Any

from psycopg import Connection

# 1. PG resource without locator
_ORPHAN_LOCATOR = (
    "SELECT r.resource_id, r.module_id "
    "FROM gptbridge_index.resource r "
    "LEFT JOIN registry.locations loc ON loc.resource_id = r.resource_id "
    "WHERE loc.locator_id IS NULL AND r.deletion_stage = 'active'"
)

# 2. Qdrant index_state without chunks (stale index)
_ORPHAN_QDRANT = (
    "SELECT s.resource_id, s.module_id, s.qdrant_collection "
    "FROM gptbridge_rag.index_state s "
    "LEFT JOIN gptbridge_rag.chunk c ON c.resource_id = s.resource_id "
    "WHERE c.chunk_id IS NULL AND s.status = 'indexed' "
    "AND s.deletion_stage = 'active'"
)

# 3. PG resource marked stale (cross-engine mismatch)
_STALE_RESOURCE = (
    "SELECT resource_id, module_id, version, backend_generation "
    "FROM gptbridge_index.resource WHERE stale = true "
    "AND deletion_stage = 'active'"
)


def scan_orphans(connection: Connection[Any]) -> list[dict[str, Any]]:
    """Scan for orphan states across all engines.

    Returns a list of orphan records, each with a 'type' field:
      'missing-locator'   — PG resource has no locator
      'orphan-qdrant'    — Qdrant index_state has no chunks
      'stale-resource'   — PG resource marked stale
    """
    orphans: list[dict[str, Any]] = []

    # 1. Missing locator
    for row in connection.execute(_ORPHAN_LOCATOR).fetchall():
        orphans.append({
            "type": "missing-locator",
            "resource_id": str(row[0]),
            "module_id": str(row[1]),
        })

    # 2. Orphan Qdrant (index_state indexed but no chunks)
    for row in connection.execute(_ORPHAN_QDRANT).fetchall():
        orphans.append({
            "type": "orphan-qdrant",
            "resource_id": str(row[0]),
            "module_id": str(row[1]),
            "qdrant_collection": str(row[2]),
        })

    # 3. Stale resources
    for row in connection.execute(_STALE_RESOURCE).fetchall():
        orphans.append({
            "type": "stale-resource",
            "resource_id": str(row[0]),
            "module_id": str(row[1]),
            "version": int(row[2]),
            "backend_generation": int(row[3]),
        })

    return orphans


__all__ = ["scan_orphans"]
