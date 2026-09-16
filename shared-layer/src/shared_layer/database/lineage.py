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
        record_rag_resource, record_qdrant_point,
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
    qdrant:point:{point_id}       --engine=qdrant       node_type=point

    resource ─chunk_of─▶ chunk ─embedded_from─▶ point
     resource ─indexed_from───────────────▶ point

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A46/E22 — Audit: mandatory-ledger; write=governed-executor.
    A10/E10 — Authorization: explicit-allowlist; deny-by-default.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from psycopg import Connection

from shared_layer.database.provenance import set_provenance, clear_provenance

_logger = logging.getLogger("gptbridge.lineage")

RELATION_TYPES: tuple[str, ...] = (
    "derived_from",
    "chunk_of",
    "indexed_from",
    "embedded_from",
    "copied_from",
    "supersedes",
    "reconciled_from",
    "restored_from",
    "produced_by",
    "invalidates",
    "corrects",
)

NODE_ENGINES: tuple[str, ...] = (
    "postgresql",
    "sqlite",
    "qdrant",
    "transport",
    "audit",
    "rag",
    "file",
    "model",
)

PRODUCER_TYPES: tuple[str, ...] = (
    "user",
    "model",
    "executor",
    "system",
    "reconcile",
    "restore",
)

CONTENT_STATUSES: tuple[str, ...] = (
    "derived",
    "candidate",
    "reviewed",
    "accepted",
    "authoritative",
)


def resource_node_id(module_id: str, resource_id: str) -> str:
    """Standard node id for a PostgreSQL canonical resource."""
    return f"pg:{module_id}:{resource_id}"


def chunk_node_id(chunk_id: str) -> str:
    """Standard node id for a RAG chunk."""
    return f"rag:chunk:{chunk_id}"


def point_node_id(point_id: str) -> str:
    """Standard node id for a Qdrant point."""
    return f"qdrant:point:{point_id}"


def _query(connection: Connection[Any]) -> "psycopg.Cursor":
    """Small guard so every function talks through a live cursor."""
    return connection.cursor()


def ensure_transformation(
    connection: Connection[Any],
    *,
    transformation_id: str,
    transformation_type: str,
    implementation_version: str,
    input_contract: Optional[str] = None,
    output_contract: Optional[str] = None,
    executor: Optional[str] = None,
) -> None:
    """Idempotently register a transformation in the lineage registry."""
    with _query(connection) as cur:
        cur.execute(
            "SELECT gptbridge_lineage.ensure_transformation(%s,%s,%s,%s,%s,%s)",
            (
                transformation_id,
                transformation_type,
                implementation_version,
                input_contract,
                output_contract,
                executor,
            ),
        )


def register_node(
    connection: Connection[Any],
    *,
    node_id: str,
    engine: str,
    node_type: str,
    module_id: Optional[str] = None,
    resource_id: Optional[str] = None,
    chunk_id: Optional[str] = None,
    qdrant_point_id: Optional[str] = None,
    origin_type: Optional[str] = None,
    origin_locator: Optional[str] = None,
    origin_revision: Optional[int] = None,
    generation_id: Optional[str] = None,
    content_hash: Optional[str] = None,
    producer_actor: Optional[str] = None,
    producer_executor: Optional[str] = None,
    producer_type: str = "executor",
    model_id: Optional[str] = None,
    model_version: Optional[str] = None,
    confidence: Optional[float] = None,
    content_status: str = "derived",
    authority_class: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> str:
    """Register a lineage node (idempotent). Returns the node id."""
    with _query(connection) as cur:
        cur.execute(
            """
            SELECT gptbridge_lineage.register_node(
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            """,
            (
                node_id,
                engine,
                node_type,
                module_id,
                resource_id,
                chunk_id,
                qdrant_point_id,
                origin_type,
                origin_locator,
                origin_revision,
                generation_id,
                content_hash,
                producer_actor,
                producer_executor,
                producer_type,
                model_id,
                model_version,
                confidence,
                content_status,
                authority_class,
                metadata,
            ),
        )
        return node_id


def relate(
    connection: Connection[Any],
    *,
    parent_node_id: str,
    child_node_id: str,
    relation_type: str,
    transformation_id: Optional[str] = None,
    run_id: Optional[str] = None,
    module_id: Optional[str] = None,
    generation_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    """Create an append-only edge between two nodes. Returns the edge id."""
    if relation_type not in RELATION_TYPES:
        raise ValueError(f"unknown lineage relation_type: {relation_type!r}")
    with _query(connection) as cur:
        cur.execute(
            "SELECT gptbridge_lineage.relate(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                parent_node_id,
                child_node_id,
                relation_type,
                transformation_id,
                run_id,
                module_id,
                generation_id,
                metadata,
            ),
        )
        row = cur.fetchone()
        return int(row[0])


def invalidate(
    connection: Connection[Any],
    *,
    node_id: str,
    reason: str,
    invalidated_by: Optional[str] = None,
    run_id: Optional[str] = None,
    module_id: Optional[str] = None,
    generation_id: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> int:
    """Append-only invalidation: record 'invalidates' arcs over the subtree.

    Returns the number of edges invalidated (0 means nothing was affected).
    """
    with _query(connection) as cur:
        cur.execute(
            "SELECT gptbridge_lineage.invalidate(%s,%s,%s,%s,%s,%s,%s)",
            (
                node_id,
                reason,
                invalidated_by,
                run_id,
                module_id,
                generation_id,
                metadata,
            ),
        )
        row = cur.fetchone()
        return int(row[0])


def record_reconcile(
    connection: Connection[Any],
    *,
    parent_node_id: str,
    child_node_id: str,
    correction_type: str,
    run_id: str,
    module_id: Optional[str] = None,
    generation_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
    actor: Optional[str] = None,
) -> int:
    """Record a reconcile correction (reconciled_from edge). Returns edge id."""
    with _query(connection) as cur:
        cur.execute(
            "SELECT gptbridge_lineage.record_reconcile(%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                parent_node_id,
                child_node_id,
                correction_type,
                run_id,
                module_id,
                generation_id,
                details,
                actor,
            ),
        )
        row = cur.fetchone()
        return int(row[0])


def record_restore(
    connection: Connection[Any],
    *,
    parent_node_id: str,
    child_node_id: str,
    run_id: str,
    module_id: Optional[str] = None,
    generation_id: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    details: Optional[dict[str, Any]] = None,
) -> int:
    """Record a restore/rollback provenance arc. Returns edge id."""
    with _query(connection) as cur:
        cur.execute(
            "SELECT gptbridge_lineage.record_restore(%s,%s,%s,%s,%s,%s,%s)",
            (
                parent_node_id,
                child_node_id,
                run_id,
                module_id,
                generation_id,
                snapshot_id,
                details,
            ),
        )
        row = cur.fetchone()
        return int(row[0])


def _walk(
    connection: Connection[Any],
    function: str,
    node_id: str,
    max_depth: int,
) -> list[dict[str, Any]]:
    with _query(connection) as cur:
        cur.execute(
            f"SELECT * FROM gptbridge_lineage.{function}(%s, %s)",
            (node_id, max_depth),
        )
        columns = [d.name for d in cur.description]
        rows = []
        for row in cur.fetchall():
            rows.append(dict(zip(columns, row, strict=True)))
        return rows


def reverse_lookup(
    connection: Connection[Any],
    node_id: str,
    max_depth: int = 10,
) -> list[dict[str, Any]]:
    """Trace a node back to its sources (parent nodes)."""
    return _walk(connection, "reverse_lookup", node_id, max_depth)


def impact(
    connection: Connection[Any],
    node_id: str,
    max_depth: int = 10,
) -> list[dict[str, Any]]:
    """Trace a node forward to every derived/dependent item."""
    return _walk(connection, "impact", node_id, max_depth)


def health_check(connection: Connection[Any]) -> list[dict[str, Any]]:
    """Return lineage integrity report rows (category/severity/entity/detail)."""
    with _query(connection) as cur:
        cur.execute("SELECT * FROM gptbridge_lineage.health_check()")
        columns = [d.name for d in cur.description]
        return [
            dict(zip(columns, row, strict=True))
            for row in cur.fetchall()
        ]


# ============================================================================
# High-level RAG helpers (wired into CanonicalRagBackend / OutboxWorker).
# ============================================================================

def record_rag_resource(
    connection: Connection[Any],
    *,
    module_id: str,
    resource_id: str,
    generation_id: str,
    source_version: int,
    content_hash: str,
    chunk_ids: list[str],
    chunk_hashes: Optional[list[str]] = None,
    chunk_policy_version: str = "v3",
    run_id: Optional[str] = None,
    actor_id: Optional[str] = None,
    executor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    decision_id: Optional[str] = None,
    producer_type: str = "executor",
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    """Record a RAG ingestion: resource node + chunk nodes + chunk_of edges.

    All inside the caller's transaction (atomic with the metadata write).
    Returns a map of registered node ids for later edge wiring.
    """
    res_node = resource_node_id(module_id, resource_id)
    register_node(
        connection,
        node_id=res_node,
        engine="postgresql",
        node_type="resource",
        module_id=module_id,
        resource_id=resource_id,
        generation_id=generation_id,
        origin_revision=source_version,
        content_hash=content_hash,
        producer_actor=actor_id,
        producer_executor=executor_id,
        producer_type=producer_type,
        authority_class="canonical",
        metadata=metadata,
    )

    transformation_id = (
        f"rag.chunking.{chunk_policy_version}"
        if chunk_policy_version
        else "rag.chunking.unknown"
    )
    ensure_transformation(
        connection,
        transformation_id=transformation_id,
        transformation_type="chunking",
        implementation_version=chunk_policy_version or "unknown",
        input_contract="resource",
        output_contract="chunk",
        executor=executor_id or "gptbridge_rag_executor",
    )

    chunk_nodes: dict[str, str] = {}
    for i, chunk_id in enumerate(chunk_ids):
        chunk_node = chunk_node_id(chunk_id)
        register_node(
            connection,
            node_id=chunk_node,
            engine="rag",
            node_type="chunk",
            module_id=module_id,
            resource_id=resource_id,
            chunk_id=chunk_id,
            generation_id=generation_id,
            origin_revision=source_version,
            content_hash=chunk_hashes[i] if chunk_hashes else None,
            producer_actor=actor_id,
            producer_executor=executor_id,
            producer_type="executor",
            content_status="derived",
            metadata={"sequence": i},
        )
        relate(
            connection,
            parent_node_id=res_node,
            child_node_id=chunk_node,
            relation_type="chunk_of",
            transformation_id=transformation_id,
            run_id=run_id,
            module_id=module_id,
            generation_id=generation_id,
            metadata={"sequence": i},
        )
        chunk_nodes[chunk_id] = chunk_node

    return {"resource_node": res_node, "chunk_nodes": chunk_nodes}


def record_qdrant_point(
    connection: Connection[Any],
    *,
    point_id: str,
    chunk_id: str,
    module_id: str,
    resource_id: str,
    generation_id: str,
    embedding_model: Optional[str] = None,
    embedding_dimension: Optional[int] = None,
    run_id: Optional[str] = None,
    source_version: Optional[int] = None,
    content_hash: Optional[str] = None,
    actor_id: Optional[str] = None,
    executor_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    decision_id: Optional[str] = None,
    producer_type: str = "model",
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    """Record an applied Qdrant point and its source arcs.

    point ──embedded_from──▶ chunk ──chunk_of──▶ resource (via reverse links).
    Idempotent: the entry-point edges use a stable run_id so re-application
    (outbox retries / reconciles) never duplicates the graph.
    """
    point_node = point_node_id(point_id)

    ensure_transformation(
        connection,
        transformation_id=f"rag.embedding.{embedding_model or 'unknown'}",
        transformation_type="embedding",
        implementation_version="1",
        input_contract=f"chunk:{chunk_id}",
        output_contract=f"vector:{embedding_dimension or '?'}",
        executor=executor_id or "gptbridge_rag_executor",
    )

    register_node(
        connection,
        node_id=point_node,
        engine="qdrant",
        node_type="point",
        module_id=module_id,
        resource_id=resource_id,
        chunk_id=chunk_id,
        qdrant_point_id=point_id,
        generation_id=generation_id,
        origin_revision=source_version,
        content_hash=content_hash,
        producer_actor=actor_id,
        producer_executor=executor_id,
        producer_type=producer_type,
        model_id=embedding_model,
        model_version=embedding_model,
        confidence=1.0,
        content_status="derived",
        metadata=metadata,
    )

    chunk_node = chunk_node_id(chunk_id)
    res_node = resource_node_id(module_id, resource_id)

    relate(
        connection,
        parent_node_id=point_node,
        child_node_id=chunk_node,
        relation_type="embedded_from",
        transformation_id=f"rag.embedding.{embedding_model or 'unknown'}",
        run_id=run_id,
        module_id=module_id,
        generation_id=generation_id,
        metadata={"point_id": point_id},
    )
    relate(
        connection,
        parent_node_id=point_node,
        child_node_id=res_node,
        relation_type="indexed_from",
        transformation_id=f"rag.embedding.{embedding_model or 'unknown'}",
        run_id=run_id,
        module_id=module_id,
        generation_id=generation_id,
        metadata={"point_id": point_id},
    )

    return {"point_node": point_node, "chunk_node": chunk_node, "resource_node": res_node}


@contextmanager
def best_effort(*log_kwargs_keys: str) -> Iterator[None]:
    """Context guard for lineage calls that must never break the write path.

    Usage:
        with lineage_best_effort("module_id", "resource_id"):
            record_rag_resource(conn, ...)

    Any exception raised inside is logged (via ``_logger.warning``) and
    swallowed so a lineage failure never blocks the governed write or the
    outbox pipeline. Values for the logged keys are taken from kwargs passed
    by the caller through ``**kwargs``; if no kwargs are supplied the guard
    simply logs the exception message.
    """
    try:
        yield
    except Exception as exc:  # noqa: BLE001 — lineage is best-effort
        _logger.warning("lineage best-effort record failed: %s", exc)


@contextmanager
def lineage_provenance(
    connection: Connection[Any],
    *,
    actor_id: Optional[str] = None,
    executor_id: Optional[str] = None,
    decision_id: Optional[str] = None,
    correlation_id: Optional[str] = None,
    generation: Optional[int] = None,
    source_revision: Optional[int] = None,
) -> Iterator[None]:
    """Declare provenance on a connection for the duration of the write.

    Wraps :func:`provenance.set_provenance` and clears the variables on exit
    so subsequent unrelated statements in the same session stay clean.
    """
    set_provenance(
        connection,
        actor_id=actor_id,
        executor_id=executor_id,
        decision_id=decision_id,
        correlation_id=correlation_id,
        generation=generation,
        source_revision=source_revision,
    )
    try:
        yield
    finally:
        clear_provenance(connection)


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
    "record_qdrant_point",
    "best_effort",
    "lineage_provenance",
]