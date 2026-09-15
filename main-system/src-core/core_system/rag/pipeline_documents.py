"""Document-level canonical write path (A371-A374).

Fixed flow: resource → PostgreSQL metadata → chunk → Qdrant →
qdrant_point_id 回寫 PostgreSQL.  PostgreSQL is the metadata/chunk/
index_state authority and never stores vectors; Qdrant stores dense
vectors only.  index_state is written back only after Qdrant confirms
the upsert.  Any failed or degraded write leaves a durable
pending_rag_mutation for idempotent replay.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from qdrant_client.http.models import PointStruct

from .rag_qdrant import IndexState
from .runtime_state import RagRuntimeState

_logger = logging.getLogger("gptbridge.rag")


class PipelineDocumentsMixin:
    """Document write flow + tombstone gate + index_state writeback."""

    async def index_document(
        self,
        *,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        collection_dimension: Optional[int] = None,
    ) -> bool:
        """Document-level canonical write (A371-A374).

        ``chunks`` carry deterministic ``qdrant_point_id``/``point_id``
        UUIDs and self-describing payloads.
        """
        await self.attempt_recovery()
        module_id = str(document["module_id"])
        resource_id = str(document["resource_id"])
        if self.state == RagRuntimeState.DEGRADED:
            # A374: record the pending_rag_mutation so recovery replays it
            # through the real re-fetch → re-chunk → re-embed → write flow.
            await self._enqueue_document_mutation(document, chunks)
            return False
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        if collection_dimension:
            self.config.embedding_dimension = int(collection_dimension)

        if await self._tombstoned(module_id, resource_id):
            return False

        embedding_model = str(
            document.get("embedding_model") or self.config.embedding_model
        )
        if not await self._canonical_document_write(
            document, chunks, vectors, resource_id, module_id,
            embedding_model, collection_dimension,
        ):
            # Any failed canonical step leaves a durable pending mutation
            # so recovery replays the whole write idempotently.
            await self._enqueue_document_mutation(document, chunks)
            return False
        return True

    async def _canonical_document_write(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        resource_id: str,
        module_id: str,
        embedding_model: str,
        collection_dimension: Optional[int],
    ) -> bool:
        """PG authority writes → Qdrant upsert → index_state writeback."""
        if not await self._pg_document_writes(
            document, chunks, resource_id, module_id, embedding_model
        ):
            return False
        # Step 3: Qdrant dense vector write (canonical semantic index).
        if not await self.qdrant.ensure_collection(collection_dimension):
            return False
        points = self._document_points(document, chunks, vectors, module_id, resource_id)
        if points and not await self.qdrant.upsert_points(points):
            return False
        # Step 4: qdrant_point_id 回寫 PostgreSQL (index_state writeback).
        return await self._writeback_index_state(
            document, chunks, resource_id, module_id, embedding_model,
            collection_dimension,
        )

    async def _tombstoned(self, module_id: str, resource_id: str) -> bool:
        """A374: reject stale writes against an existing tombstone."""
        if await self.postgresql.is_tombstoned(module_id, resource_id):
            _logger.warning(
                "CanonicalRagPipeline: reject index_document for tombstoned %s:%s",
                module_id, resource_id,
            )
            return True
        return False

    async def _pg_document_writes(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        resource_id: str,
        module_id: str,
        embedding_model: str,
    ) -> bool:
        """Steps 1-2: resource row, then chunk rows in the PG authority."""
        if not await self.postgresql.ensure_resource(document):
            return False
        return await self.postgresql.replace_document_chunks(
            resource_id=resource_id,
            module_id=module_id,
            embedding_model=embedding_model,
            chunks=chunks,
        )

    def _document_points(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        module_id: str,
        resource_id: str,
    ) -> list[PointStruct]:
        """Build Qdrant PointStructs for a document's chunks."""
        return [
            PointStruct(
                id=str(chunk.get("qdrant_point_id") or chunk.get("point_id")),
                vector=[float(v) for v in vector],
                payload={
                    "module_id": module_id,
                    "document_resource_id": resource_id,
                    "document_id": document.get("document_id"),
                    "indexed_at_utc": datetime.now(timezone.utc).isoformat(),
                    **(chunk.get("payload") or {}),
                },
            )
            for chunk, vector in zip(chunks, vectors)
        ]

    async def _writeback_index_state(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        resource_id: str,
        module_id: str,
        embedding_model: str,
        collection_dimension: Optional[int],
    ) -> bool:
        """Step 4: write index_state back to PostgreSQL after Qdrant confirms."""
        first_point = str(chunks[0].get("qdrant_point_id") or chunks[0].get("point_id")) if chunks else ""
        state = IndexState(
            resource_id=resource_id,
            module_id=module_id,
            embedding_model=embedding_model,
            embedding_dimension=int(collection_dimension or self.config.embedding_dimension),
            chunk_size=int(self.config.chunk_size),
            chunk_overlap=int(self.config.chunk_overlap),
            indexed_at_utc=datetime.now(timezone.utc).isoformat(),
            content_hash=str(document.get("sha256") or document.get("content_hash") or ""),
            qdrant_point_id=first_point,
            postgresql_record_id=resource_id,
        )
        return await self.postgresql.upsert_index_state(
            state,
            collection_name=self.config.collection_name,
            chunk_count=len(chunks),
        )
