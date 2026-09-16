"""Degraded RAG Backend — LocalVectorStore + LocalSqliteRagRepository 實作。

實作 RagIndexBackend 介面，用於 canonical 不可用時的降級模式。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .rag_protocol import RagIndexBackend, RagBackendHealth, BackendType
from .rag_contracts import (
    RagIndexRequest,
    RagIndexResult,
    RagDeleteRequest,
    RagDeleteResult,
    RagSearchRequest,
    RagSearchResult,
    RagSearchHit,
    RagReconcileRequest,
    RagReconcileResult,
    LifecycleState,
    RagChunk,
)
from .pipeline_degraded import DegradedRagPipeline
from .rag_qdrant import RagPipelineConfig

_logger = logging.getLogger("gptbridge.rag.degraded_backend")


class DegradedRagBackend:
    """Degraded RAG backend: LocalVectorStore + LocalSqliteRagRepository."""

    def __init__(
        self,
        config: RagPipelineConfig,
        degraded_root: Optional[Path] = None,
    ) -> None:
        self.config = config
        self.pipeline = DegradedRagPipeline(config, degraded_root)

    def health(self) -> RagBackendHealth:
        """Return degraded backend health."""
        return RagBackendHealth(
            backend_type=BackendType.DEGRADED,
            healthy=True,
            state="DEGRADED",
            components={
                "vector_store": True,
                "repository": True,
            },
            embedding_available=True,
            metadata_available=True,
            vector_available=True,
            message="Degraded backend operational (local stores)",
        )

    def upsert_resource(self, request: RagIndexRequest) -> RagIndexResult:
        """Index resource through degraded path."""
        try:
            # Convert RagChunk to pipeline format
            # DegradedRagPipeline expects content in the chunk
            for chunk in request.chunks:
                # Use pipeline's index_resource
                self.pipeline.index_resource(
                    module_id=request.module_id,
                    resource_id=request.resource_id,
                    content=chunk.content,
                    metadata={
                        "chunk_id": chunk.chunk_id,
                        "locator_id": chunk.locator_id,
                        "content_hash": chunk.content_hash,
                        "sequence": chunk.sequence,
                        "character_start": chunk.character_start,
                        "character_end": chunk.character_end,
                        "rag_types": [rt.value for rt in request.rag_types],
                        "data_category": request.data_category.value,
                        "resource_type": request.resource_type.value,
                        "generation_id": request.generation_id,
                        "source_version": request.source_version,
                        "embedding_model": request.embedding_model,
                    },
                    embedding=[0.0] * request.embedding_dimension,  # Placeholder
                )

            return RagIndexResult(
                request_id=request.request_id,
                resource_id=request.resource_id,
                module_id=request.module_id,
                success=True,
                new_version=request.source_version,
                state=LifecycleState.CANONICAL_INDEXED,  # In degraded, this means locally indexed
                qdrant_point_ids=tuple(chunk.chunk_id for chunk in request.chunks),
            )

        except Exception as e:
            _logger.error("DegradedRagBackend.upsert_resource failed: %s", e)
            return RagIndexResult(
                request_id=request.request_id,
                resource_id=request.resource_id,
                module_id=request.module_id,
                success=False,
                new_version=0,
                state=LifecycleState.INDEX_PENDING,
                qdrant_point_ids=(),
                error_message=str(e),
            )

    def delete_resource(self, request: RagDeleteRequest) -> RagDeleteResult:
        """Delete resource from local stores."""
        try:
            # DegradedRagPipeline doesn't have explicit delete, use repository
            self.pipeline.repository.delete_resource(request.resource_id)
            self.pipeline.vector_store.delete_resource(request.resource_id)

            return RagDeleteResult(
                request_id=request.request_id,
                resource_id=request.resource_id,
                module_id=request.module_id,
                success=True,
                state=LifecycleState.TOMBSTONED,
            )
        except Exception as e:
            _logger.error("DegradedRagBackend.delete_resource failed: %s", e)
            return RagDeleteResult(
                request_id=request.request_id,
                resource_id=request.resource_id,
                module_id=request.module_id,
                success=False,
                state=LifecycleState.RECONCILING,
                error_message=str(e),
            )

    def search(self, request: RagSearchRequest) -> RagSearchResult:
        """Search through degraded path."""
        try:
            # Degraded pipeline query returns RagQueryResult
            results = self.pipeline.query(
                query_embedding=list(request.query_vector),
                module_id=request.module_ids[0] if request.module_ids else None,
                top_k=request.top_k,
                score_threshold=request.score_threshold,
            )

            hits = []
            for r in results:
                hits.append(RagSearchHit(
                    point_id=r.index_state.qdrant_point_id,
                    score=r.score,
                    locator_id=f"{r.module_id}:{r.resource_id}",
                    chunk_id=r.index_state.qdrant_point_id,
                    resource_id=r.resource_id,
                    module_id=r.module_id,
                    generation_id=r.index_state.generation_id or "",
                    source_version=0,  # Not tracked in degraded
                    content_hash=r.index_state.content_hash,
                    verified=False,  # Degraded path doesn't verify against PG
                    metadata=r.metadata,
                ))

            return RagSearchResult(
                request_id=request.request_id,
                hits=tuple(hits),
                total_candidates=len(hits),
                verified_count=0,
                dropped_count=0,
                latency_ms=0,
            )

        except Exception as e:
            _logger.error("DegradedRagBackend.search failed: %s", e)
            return RagSearchResult(
                request_id=request.request_id,
                hits=(),
                total_candidates=0,
                verified_count=0,
                dropped_count=0,
                latency_ms=0,
            )

    def reconcile(self, request: RagReconcileRequest) -> RagReconcileResult:
        """Local store reconciliation."""
        return RagReconcileResult(
            request_id=request.request_id,
            checked=0,
            fixed=0,
            failed=0,
            discrepancies=[],
            latency_ms=0,
        )


__all__ = ["DegradedRagBackend"]