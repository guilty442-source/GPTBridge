"""High-level RAG lineage helpers (wired into CanonicalRagBackend / OutboxWorker)."""
from __future__ import annotations

from typing import Any, Optional

from psycopg import Connection

from .lineage_core import (
    chunk_node_id,
    ensure_transformation,
    point_node_id,
    register_node,
    relate,
    resource_node_id,
)


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


def record_vector_point(
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
    """Record an applied vector point and its source arcs.

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
        engine="vector",
        node_type="point",
        module_id=module_id,
        resource_id=resource_id,
        chunk_id=chunk_id,
        vector_point_id=point_id,
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
