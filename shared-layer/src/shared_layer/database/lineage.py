"""Central Lineage Facade (migration 086).

Python entry point for the ``gptbridge_lineage`` append-only provenance graph.
Every content-holding entity across engines (PG resource, RAG chunk, Qdrant
point, SQLite record, audit event, model output) is a ``lineage_node``; every
"produced from / of" relation is a ``lineage_edge``; every chunking /
embedding / reconcile / restore / migration transformation is registered in
the ``gptbridge_lineage.transformation`` registry.

This module mirrors :mod:`provenance` — it writes through the same session
provenance variables (``SET LOCAL gptbridge.*`` via
``shared_layer.database.provenance.set_provenance``) and never mutates
existing rows (append-only).

Usage:
    from shared_layer.database.provenance import set_provenance
    from shared_layer.database.lineage import (
        record_rag_resource, record_vector_point,
        reverse_lookup, impact, health_check,
    )

    with connection_manager.connection() as conn:
        set_provenance(conn, actor_id="...", executor_id="...",
                       correlation_id="...", decision_id="...")
        record_rag_resource(conn, module_id=..., resource_id=..., ...)
        conn.execute("INSERT INTO gptbridge_rag.resource_versions ...")
        conn.commit()

RAG ingestion graph (conventions used by the wired call sites):
    pg:{module_id}:{resource_id}  --engine=postgresql  node_type=resource
    rag:chunk:{chunk_id}          --engine=rag          node_type=chunk
    vector:point:{point_id}       --engine=vector       node_type=point

    resource ─chunk_of─▶ chunk ─embedded_from─▶ point
     resource ─indexed_from───────────────▶ point

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A46/E22 — Audit: mandatory-ledger; write=governed-executor.
    A10/E10 — Authorization: explicit-allowlist; deny-by-default.
"""
from __future__ import annotations


from .lineage_core import (
    CONTENT_STATUSES,
    NODE_ENGINES,
    PRODUCER_TYPES,
    RELATION_TYPES,
    chunk_node_id,
    ensure_transformation,
    health_check,
    impact,
    invalidate,
    point_node_id,
    record_reconcile,
    record_restore,
    register_node,
    relate,
    resource_node_id,
    reverse_lookup,
)
from .lineage_guards import best_effort, lineage_provenance
from .lineage_rag import record_rag_resource, record_vector_point


__all__ = [
    "RELATION_TYPES",
    "NODE_ENGINES",
    "PRODUCER_TYPES",
    "CONTENT_STATUSES",
    "resource_node_id",
    "chunk_node_id",
    "point_node_id",
    "ensure_transformation",
    "register_node",
    "relate",
    "invalidate",
    "record_reconcile",
    "record_restore",
    "reverse_lookup",
    "impact",
    "health_check",
    "record_rag_resource",
    "record_vector_point",
    "best_effort",
    "lineage_provenance",
]