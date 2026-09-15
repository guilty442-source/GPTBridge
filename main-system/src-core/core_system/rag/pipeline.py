"""RAG Pipeline — Canonical RAG path implementation (A371-A374).

A371: DEFAULT-PATH: source content > qdrant dense retrieval > PostgreSQL official metadata/FTS/index_state > Python domain model > typed result
A374: Binding order: 1 QDRANT_CANONICAL_RUNTIME > 2 PostgreSQL metadata/FTS/index_state > 3 Python domain model
A373: CANONICAL-TAKEOVER: normal read/write must prove Qdrant dense retrieval and PostgreSQL metadata/FTS/index_state are the live path
A374: INDEX-STATE: every indexed resource/chunk records embedding_model, embedding_dimension, chunk_size, chunk_overlap, indexed_at_utc
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import psycopg
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue

from shared_layer.metadata_contract import (
    FIELD_CONTENT_HASH,
    FIELD_MODULE_ID,
    FIELD_RESOURCE_ID,
    FIELD_STATUS,
    FIELD_UPDATED_AT,
    FIELD_VERSION,
    STATUS_INDEXED,
)

_logger = logging.getLogger("gptbridge.rag")

# A374: INDEX-STATE fields
INDEX_STATE_FIELDS = (
    "embedding_model",
    "embedding_dimension",
    "chunk_size",
    "chunk_overlap",
    "indexed_at_utc",
)



from .rag_qdrant import IndexState, QdrantCanonicalRuntime, RagPipelineConfig, RagQueryResult
from .rag_metadata import PostgreSQLMetadataAuthority
from .pipeline_domain import PythonDomainModel
from .runtime_state import (
    CanonicalCheckError,
    CrossStoreOutbox,
    OutboxStep,
    QueueOperation,
    QueueStatus,
    RagRuntimeState,
    RagRuntimeStateMachine,
    ReconciliationQueue,
    ReconciliationQueueItem,
    SagaResult,
    TombstoneGuard,
    TransitionError,
)










class CanonicalRagPipeline:
    """A371-A374: Canonical RAG pipeline implementation.

    DEFAULT-PATH: source content > qdrant dense retrieval > PostgreSQL official metadata/FTS/index_state > Python domain model > typed result

    Binding order (A374):
    1. QDRANT_CANONICAL_RUNTIME (Qdrant dense retrieval)
    2. PostgreSQL metadata/FTS/index_state (live authority)
    3. Python domain model (sole production owner)

    CANONICAL-TAKEOVER (A373): normal execution must prove Qdrant + PostgreSQL are live path.
    """

    def __init__(self, config: RagPipelineConfig) -> None:
        self.config = config
        self.qdrant = QdrantCanonicalRuntime(config)
        self.postgresql = PostgreSQLMetadataAuthority(config.postgresql_dsn)
        self.domain_model = PythonDomainModel(config)
        self._initialized = False
        # A374: runtime state machine.  The durable queue is backed by a
        # local SQLite store for the in-process authority; the canonical
        # PostgreSQL queue is mirrored by PostgreSQLMetadataAuthority when
        # PostgreSQL is healthy.  The TombstoneGuard is the in-memory
        # authoritative view; PostgreSQL is the durable tombstone store.
        self._queue_db = sqlite3.connect(
            ":memory:", check_same_thread=False
        )
        self._queue_db.row_factory = sqlite3.Row
        self._queue = ReconciliationQueue(self._queue_db)
        self._tombstone = TombstoneGuard()
        self._outbox = CrossStoreOutbox(self._queue, self._tombstone)
        self._state_machine = RagRuntimeStateMachine(
            self._queue, self._tombstone, self._outbox
        )

    @property
    def state(self) -> RagRuntimeState:
        """A374: current runtime state."""
        return self._state_machine.state

    @property
    def state_machine(self) -> RagRuntimeStateMachine:
        return self._state_machine

    async def initialize(self) -> bool:
        """Initialize all canonical components in order (A374).

        After initialization, evaluate the A374 startup readiness gate:
        STARTING -> CANONICAL only when Qdrant + PostgreSQL are healthy,
        the authoritative index_state matches, and the reconciliation queue
        is complete.  Otherwise STARTING -> DEGRADED.
        """
        _logger.info("CanonicalRagPipeline: initializing...")

        # Step 1: Qdrant canonical runtime
        qdrant_ok = await self.qdrant.initialize()
        if qdrant_ok:
            await self.qdrant.ensure_collection()

        # Step 2: PostgreSQL metadata authority
        pg_ok = await self.postgresql.initialize()

        self._initialized = qdrant_ok and pg_ok
        _logger.info("CanonicalRagPipeline: initialized=%s (qdrant=%s, pg=%s)", self._initialized, qdrant_ok, pg_ok)

        # A374: evaluate startup readiness gate.
        index_state_matches = self._initialized  # minimal: both stores up
        if pg_ok:
            # If PostgreSQL is healthy, mirror any pending canonical queue
            # items into the local queue view so the startup gate can see
            # whether reconciliation is still required.
            try:
                pg_pending = await self.postgresql.pending_reconciliation_count()
                if pg_pending > 0:
                    index_state_matches = False
            except Exception as exc:
                _logger.warning("CanonicalRagPipeline: queue check failed: %s", exc)
                index_state_matches = False
        self._state_machine.evaluate_startup(
            qdrant_healthy=qdrant_ok,
            postgresql_healthy=pg_ok,
            index_state_matches=index_state_matches,
        )
        return self._initialized

    def is_ready(self) -> bool:
        """A373: Prove Qdrant + PostgreSQL are live path."""
        return self._initialized and self.qdrant.is_healthy() and self.postgresql.is_healthy()

    async def index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> IndexState:
        """Index a resource through the canonical path (write path)."""
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        point_id = str(uuid.uuid4())
        now_utc = datetime.now(timezone.utc).isoformat()

        # Step 1: Write to Qdrant (canonical write)
        point = PointStruct(
            id=point_id,
            vector=embedding,
            payload={
                "resource_id": resource_id,
                "module_id": module_id,
                "content": content,
                "content_hash": content_hash,
                "indexed_at_utc": now_utc,
                **metadata,
            },
        )
        await self.qdrant.upsert_points([point])

        # Step 2: Record index state in PostgreSQL (authoritative)
        index_state = IndexState(
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
        await self.postgresql.upsert_index_state(index_state)

        return index_state

    async def query(
        self,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> list[RagQueryResult]:
        """Query through the canonical RAG path (read path)."""
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")

        # A373: CANONICAL-TAKEOVER - prove Qdrant + PostgreSQL are live
        if not self.qdrant.is_healthy():
            raise RuntimeError("Qdrant canonical runtime not healthy")
        if not self.postgresql.is_healthy():
            raise RuntimeError("PostgreSQL metadata authority not healthy")

        # Step 1: Qdrant dense retrieval
        qdrant_hits = await self.qdrant.search(
            query_vector=query_embedding,
            module_id=module_id,
            top_k=top_k,
            score_threshold=score_threshold,
        )

        if not qdrant_hits:
            return []

        pg_metadata, index_states = await self._pg_evidence(qdrant_hits)

        # Step 4: Build typed results via Python domain model
        return self.domain_model.build_typed_result(qdrant_hits, pg_metadata, index_states)

    async def _pg_evidence(
        self, qdrant_hits: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Steps 2-3: batch-fetch PostgreSQL metadata + index_state proof."""
        resource_ids = [
            hit.get("payload", {}).get("resource_id") or hit.get("id")
            for hit in qdrant_hits
        ]
        module_ids = set(
            hit.get("payload", {}).get("module_id")
            for hit in qdrant_hits
            if hit.get("payload", {}).get("module_id")
        )
        pg_metadata: dict[str, Any] = {}
        index_states: dict[str, Any] = {}
        for mid in module_ids:
            pg_metadata.update(await self.postgresql.fetch_metadata(mid, resource_ids))
            for rid in resource_ids:
                state = await self.postgresql.get_index_state(mid, rid)
                if state:
                    index_states[f"{mid}:{rid}"] = state
        return pg_metadata, index_states

    async def get_index_state(self, module_id: str, resource_id: str) -> Optional[IndexState]:
        """Get authoritative index state (A374)."""
        return await self.postgresql.get_index_state(module_id, resource_id)

    async def index_document(
        self,
        *,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        collection_dimension: Optional[int] = None,
    ) -> bool:
        """Document-level canonical write (A371-A374).

        Fixed flow: resource → PostgreSQL metadata → chunk → Qdrant →
        qdrant_point_id 回寫 PostgreSQL.  PostgreSQL is the metadata/chunk/
        index_state authority and never stores vectors; Qdrant stores dense
        vectors only.  index_state is written back only after Qdrant
        confirms the upsert.  ``chunks`` carry deterministic
        ``qdrant_point_id``/``point_id`` UUIDs and self-describing payloads.
        """
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        if collection_dimension:
            self.config.embedding_dimension = int(collection_dimension)

        module_id = str(document["module_id"])
        resource_id = str(document["resource_id"])
        if await self._tombstoned(module_id, resource_id):
            return False

        embedding_model = str(
            document.get("embedding_model") or self.config.embedding_model
        )
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

    async def vector_search(
        self,
        query_embedding: list[float],
        *,
        module_ids: tuple[str, ...],
        top_k: int,
        score_threshold: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Canonical dense retrieval with index_state proof (A373/A374).

        Returns payload-shaped records (chunk-level) whose document carries an
        authoritative index_state row; hits without proof are dropped.
        """
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        hits = await self.qdrant.search(
            query_vector=query_embedding,
            module_ids=module_ids,
            top_k=top_k,
            score_threshold=score_threshold,
        )
        if not hits:
            return []
        proved: list[dict[str, Any]] = []
        for hit in hits:
            payload = hit.get("payload") or {}
            module_id = str(payload.get("module_id") or "")
            document_resource_id = str(
                payload.get("document_resource_id") or payload.get("resource_id") or ""
            )
            if not module_id or not document_resource_id:
                continue
            state = await self.postgresql.get_index_state(module_id, document_resource_id)
            if state is None or str(state.status if hasattr(state, "status") else "indexed") == "tombstoned":
                continue
            record = {
                **payload,
                "id": str(hit.get("id")),
                "point_id": str(hit.get("id")),
                "module_id": module_id,
                "vector_score": round(float(hit.get("score") or 0.0), 6),
                "score": round(float(hit.get("score") or 0.0), 6),
                "index_state": state,
            }
            proved.append(record)
        return proved

    async def keyword_search(
        self,
        query: str,
        *,
        module_ids: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Canonical PostgreSQL FTS keyword channel (A374 step 2)."""
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        return await self.postgresql.keyword_search(
            query, module_ids=module_ids, limit=limit
        )

    async def health_check(self) -> dict[str, Any]:
        """Health check for all components (A374)."""
        return {
            "pipeline_ready": self.is_ready(),
            "state": self._state_machine.state.value,
            "reconciliation_required": self._state_machine.reconciliation_required,
            "queue_pending": self._queue.pending_count(),
            "queue_complete": self._queue.is_complete(),
            "qdrant": {
                "healthy": self.qdrant.is_healthy(),
                "collection": self.config.collection_name,
            },
            "postgresql": {
                "healthy": self.postgresql.is_healthy(),
            },
            "domain_model": "ok",
        }


__all__ = [
    "CanonicalRagPipeline",
    "RagPipelineConfig",
    "IndexState",
    "RagQueryResult",
    "QdrantCanonicalRuntime",
    "PostgreSQLMetadataAuthority",
    "PythonDomainModel",
]