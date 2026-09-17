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

from .rag_contracts import OutboxOperation, OutboxState
from .rag_qdrant import IndexState, sanitize_payload
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
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
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
        """PG authority writes + outbox event (ONE transaction) ->
        Qdrant upsert -> outbox SUCCEEDED + index_state writeback.

        RAG-08: the outbox event commits with the metadata, so a crash
        after commit leaves a durable PENDING event that ``process_outbox``
        replays idempotently.  Qdrant is never part of the PG transaction.
        """
        event = self._new_outbox_event(
            operation=OutboxOperation.UPSERT_RESOURCE,
            module_id=module_id,
            resource_id=resource_id,
            source_version=int(document.get("version") or 0),
            content_hash=str(
                document.get("sha256") or document.get("content_hash") or ""
            ),
            generation_id=str(document.get("generation_id") or ""),
            request_id=str(document.get("request_id") or "") or None,
            payload={
                "chunk_ids": [str(c.get("chunk_id")) for c in chunks],
            },
        )
        write_tx = getattr(self.postgresql, "document_write_tx", None)
        if write_tx is not None:
            if not await write_tx(
                document=document,
                chunks=chunks,
                embedding_model=embedding_model,
                outbox_event=event,
            ):
                return False
        else:
            if not await self._pg_document_writes(
                document, chunks, resource_id, module_id, embedding_model
            ):
                return False
            insert = getattr(self.postgresql, "insert_outbox_event", None)
            if insert is not None:
                await insert(event)
        # Step 3: Qdrant dense vector write (canonical semantic index).
        if not await self.qdrant.ensure_collection(collection_dimension):
            return False
        points = self._document_points(document, chunks, vectors, module_id, resource_id)
        if not (points and await self.qdrant.upsert_points(points)):
            # Outbox event stays PENDING — replay applies the vector write.
            mark = getattr(self.postgresql, "mark_outbox", None)
            if mark is not None:
                await mark(
                    event["event_id"], OutboxState.RETRY.value,
                    error="qdrant upsert pending",
                    next_retry_at=datetime.now(timezone.utc).isoformat(),
                )
            return False
        # Step 4: outbox SUCCEEDED + qdrant_point_id writeback.
        mark = getattr(self.postgresql, "mark_outbox", None)
        if mark is not None:
            await mark(event["event_id"], OutboxState.SUCCEEDED.value,
                       terminal=True)
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
                payload=sanitize_payload(
                    {
                        "module_id": module_id,
                        "document_resource_id": resource_id,
                        "document_id": document.get("document_id"),
                        "indexed_at_utc": datetime.now(timezone.utc).isoformat(),
                        **(chunk.get("payload") or {}),
                    }
                ),
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
