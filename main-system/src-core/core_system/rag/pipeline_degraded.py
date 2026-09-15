"""A44 degraded-mode RAG pipeline — bounded local stores.

Used by ``CanonicalRagPipeline`` when canonical Qdrant/PostgreSQL are
unavailable (state=DEGRADED).  All operations are non-canonical and
reconciliation_required.  The local stores live in ``shared_layer.local``
(codex-native SQLite engine, A219/A37 — no external service dependency).
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from shared_layer.local.local_sqlite_rag_repository import LocalSqliteRagRepository
from shared_layer.local.vector_store import LocalVectorStore

from .rag_qdrant import IndexState, RagPipelineConfig, RagQueryResult

_logger = logging.getLogger("gptbridge.rag")


def _default_degraded_root() -> Path:
    """Return a default root for degraded stores outside the project tree."""
    return Path(tempfile.gettempdir()) / "gptbridge_degraded_rag"


class DegradedRagPipeline:
    """A44 Degraded Mode RAG pipeline using local stores.

    This pipeline is used when canonical Qdrant/PostgreSQL are unavailable.
    All operations are non-canonical and require reconciliation.
    """

    def __init__(self, config: RagPipelineConfig, degraded_root: Optional[Path] = None) -> None:
        self.config = config

        if degraded_root is None:
            degraded_root = _default_degraded_root()
        degraded_root.mkdir(parents=True, exist_ok=True)

        self.vector_store = LocalVectorStore(degraded_root, dimension=config.embedding_dimension)
        self.repository = LocalSqliteRagRepository(degraded_root)
        self.vector_store.ensure_collection(config.embedding_dimension)

        _logger.info("DegradedRagPipeline: initialized at %s", degraded_root)

    def is_ready(self) -> bool:
        """Degraded pipeline is always ready (local stores)."""
        return True

    async def index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> IndexState:
        """Index a resource through the degraded path."""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        point_id = str(uuid.uuid4())
        now_utc = datetime.now(timezone.utc).isoformat()

        point = {
            "id": point_id,
            "vector": embedding,
            "payload": {
                "resource_id": resource_id,
                "module_id": module_id,
                "content": content,
                "content_hash": content_hash,
                "indexed_at_utc": now_utc,
                **metadata,
            },
            "module_id": module_id,
        }
        self.vector_store.replace_document(resource_id, [point], module_id=module_id)
        self.repository.replace_document(
            document=self._document_record(module_id, resource_id, content, metadata, content_hash),
            chunks=[{
                "chunk_id": point_id,
                "sequence": 0,
                "character_start": 0,
                "character_end": len(content),
                "point_id": point_id,
                "resource_label": resource_id,
                "content": content,
            }],
        )
        return IndexState(
            resource_id=resource_id,
            module_id=module_id,
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            indexed_at_utc=now_utc,
            content_hash=content_hash,
            qdrant_point_id=point_id,
        )

    def _document_record(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        content_hash: str,
    ) -> dict[str, Any]:
        """Repository document record for the degraded path."""
        return {
            "resource_id": resource_id,
            "module_id": module_id,
            "document_id": resource_id,
            "source": content,
            "title": metadata.get("title", ""),
            "character_count": len(content),
            "chunk_count": 1,
            "embedding_model": self.config.embedding_model,
            "owner_id": module_id,
            "platform_id": "",
            "data_category": "degraded",
            "resource_type": "document",
            "resource_label": resource_id,
            "classification": "degraded",
            "locator_id": f"{module_id}:{resource_id}",
            "sha256": content_hash,
            "version": 1,
        }

    async def query(
        self,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> list[RagQueryResult]:
        """Query through the degraded path."""
        module_ids = (module_id,) if module_id else ()
        hits = self.vector_store.query(
            query_embedding,
            limit=top_k or self.config.top_k,
            module_ids=module_ids,
        )
        if score_threshold is not None:
            hits = [h for h in hits if h.get("score", 0) >= score_threshold]
        return [r for hit in hits if (r := self._result(hit)) is not None]

    def _result(self, hit: dict[str, Any]) -> Optional[RagQueryResult]:
        """Build a typed result from a local vector hit (no PG metadata).

        ``LocalVectorStore.query`` returns flat rows — the stored payload
        fields are promoted to the top level alongside ``document_id``,
        ``point_id``, ``vector_score`` and ``score``.
        """
        resource_id = hit.get("resource_id") or hit.get("document_id")
        module_id = hit.get("module_id")
        if not resource_id or not module_id:
            return None
        return RagQueryResult(
            resource_id=resource_id,
            module_id=module_id,
            content=hit.get("content", ""),
            score=hit.get("score", 0.0),
            metadata=hit,
            index_state=IndexState(
                resource_id=resource_id,
                module_id=module_id,
                embedding_model=self.config.embedding_model,
                embedding_dimension=self.config.embedding_dimension,
                chunk_size=self.config.chunk_size,
                chunk_overlap=self.config.chunk_overlap,
                indexed_at_utc=hit.get("indexed_at_utc", ""),
                content_hash=hit.get("content_hash", ""),
                qdrant_point_id=hit.get("point_id", ""),
            ),
        )

    async def health_check(self) -> dict[str, Any]:
        """Health check for degraded pipeline."""
        return {
            "pipeline_ready": True,
            "degraded": True,
            "canonical": False,
            "reconciliation_required": True,
            "vector_store": self.vector_store.status(),
            "repository": self.repository.status(),
        }


__all__ = ["DegradedRagPipeline"]
