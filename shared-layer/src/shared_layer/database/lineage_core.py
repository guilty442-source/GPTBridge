"""Central lineage core: node/edge/transformation primitives (migration 086)."""
from __future__ import annotations

from typing import Any, Optional

from psycopg import Connection


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
    "vector",
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
    """Standard node id for a vector point."""
    return f"vector:point:{point_id}"


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
    vector_point_id: Optional[str] = None,
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
                vector_point_id,
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
        cur.execute(  # sql-ok: function name comes only from the fixed reverse_lookup/impact callers
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
        cur.execute(  # sql-ok: fixed catalog function, no parameters
            "SELECT * FROM gptbridge_lineage.health_check()"
        )
        columns = [d.name for d in cur.description]
        return [
            dict(zip(columns, row, strict=True))
            for row in cur.fetchall()
        ]
