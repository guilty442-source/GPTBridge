"""RAG Pipeline — canonical RAG path (A371-A374) + A44 degraded delegation.

A371/A374: Qdrant dense retrieval > PostgreSQL metadata/FTS/index_state >
Python domain model > typed result.  A373: canonical read/write must prove
both stores are live; DEGRADED state delegates to ``DegradedRagPipeline``.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from qdrant_client.http.models import PointStruct

_logger = logging.getLogger("gptbridge.rag")


from .rag_qdrant import (
    IndexState,
    QdrantCanonicalRuntime,
    RagPipelineConfig,
    RagQueryResult,
    sanitize_payload,
)
from .rag_metadata import PostgreSQLMetadataAuthority
from .pipeline_degraded import DegradedRagPipeline
from .pipeline_documents import PipelineDocumentsMixin
from .pipeline_domain import PythonDomainModel
from .pipeline_recovery import PipelineRecoveryMixin
from .pipeline_retrieval import PipelineRetrievalMixin
from .runtime_state import (
    CrossStoreOutbox,
    RagRuntimeState,
    RagRuntimeStateMachine,
    ReconciliationQueue,
    TombstoneGuard,
)


class CanonicalRagPipeline(
    PipelineRetrievalMixin,
    PipelineRecoveryMixin,
    PipelineDocumentsMixin,
):
    """A371-A374: canonical path — Qdrant dense retrieval > PostgreSQL
    metadata/FTS/index_state authority > domain model > typed result.
    A373: normal execution must prove Qdrant + PostgreSQL are live."""

    def __init__(
        self,
        config: RagPipelineConfig,
        *,
        document_fetcher: Optional[Any] = None,
        embed_texts: Optional[Any] = None,
    ) -> None:
        self.config = config
        self.qdrant = QdrantCanonicalRuntime(config)
        self.postgresql = PostgreSQLMetadataAuthority(config.postgresql_dsn)
        self.domain_model = PythonDomainModel(config)
        self._initialized = False
        self._degraded_pipeline: Optional[DegradedRagPipeline] = None
        # A374 reconciliation data flow: the fetcher re-reads the owning
        # module's original content and the embedder re-embeds through the
        # governed local runtime (qwen3-embedding:4b / 2560d).  Degraded
        # vectors are never replayed into the canonical collection.
        self._document_fetcher = document_fetcher
        self._embed_texts = embed_texts
        # A374: runtime state machine.  The durable queue is backed by a
        # local SQLite store for the in-process authority; the canonical
        # PostgreSQL queue is mirrored by PostgreSQLMetadataAuthority when
        # PostgreSQL is healthy.  The TombstoneGuard is the in-memory
        # authoritative view; PostgreSQL is the durable tombstone store.
        queue_path = config.queue_db_path or ":memory:"
        if queue_path != ":memory:":
            Path(queue_path).parent.mkdir(parents=True, exist_ok=True)
        self._queue_db = sqlite3.connect(
            queue_path, check_same_thread=False
        )
        self._queue_db.row_factory = sqlite3.Row
        self._queue = ReconciliationQueue(self._queue_db)
        self._tombstone = TombstoneGuard()
        self._outbox = CrossStoreOutbox(self._queue, self._tombstone)
        self._state_machine = RagRuntimeStateMachine(
            self._queue, self._tombstone, self._outbox
        )
        # Canonical takeover: sticky hard-contract violation (dimension
        # mismatch, non-loopback Qdrant URL, unverifiable contract).  While
        # set the surface state is BLOCKED and canonical reads/writes raise
        # instead of silently delegating to the degraded backend.
        self._blocked_reason: Optional[str] = None

    @property
    def state(self) -> RagRuntimeState:
        """A374: current runtime state."""
        return self._state_machine.state

    @property
    def state_machine(self) -> RagRuntimeStateMachine:
        return self._state_machine

    def _get_degraded_pipeline(self) -> DegradedRagPipeline:
        """Lazily initialize and return the degraded pipeline."""
        if self._degraded_pipeline is None:
            root = Path(self.config.degraded_root) if self.config.degraded_root else None
            self._degraded_pipeline = DegradedRagPipeline(
                self.config,
                degraded_root=root,
                enqueue_mutation=self._enqueue_degraded_write,
            )
        return self._degraded_pipeline

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
        if self.qdrant.collection_error or (
            self.qdrant.last_error or ""
        ).startswith("QDRANT_URL_NOT_LOOPBACK"):
            # Hard contract violation — BLOCKED, never silently degraded.
            self._blocked_reason = (
                self.qdrant.collection_error or self.qdrant.last_error
            )

        # Step 2: PostgreSQL metadata authority
        pg_ok = await self.postgresql.initialize()

        self._initialized = (
            qdrant_ok and pg_ok and self._blocked_reason is None
        )
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

    @property
    def blocked_reason(self) -> Optional[str]:
        return self._blocked_reason

    def gateway_state(self) -> str:
        """Canonical takeover surface state (BOOTSTRAPPING/CANONICAL_READY/
        DEGRADED_READY/RECONCILING/BLOCKED).  The internal A374 machine keeps
        exactly four states; this maps them to the governed surface names."""
        if self._blocked_reason:
            return "BLOCKED"
        mapping = {
            "STARTING": "BOOTSTRAPPING",
            "CANONICAL": "CANONICAL_READY",
            "DEGRADED": "DEGRADED_READY",
            "RECONCILING": "RECONCILING",
            "RECONCILIATION_FAILED": "DEGRADED_READY",
        }
        return mapping.get(self._state_machine.effective_state, "BOOTSTRAPPING")

    def is_ready(self) -> bool:
        """A373: Prove Qdrant + PostgreSQL are live path."""
        return (
            self._initialized
            and self._blocked_reason is None
            and self.qdrant.is_healthy()
            and self.postgresql.is_healthy()
        )

    async def index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> IndexState:
        """Index a resource through the canonical or degraded path depending on state."""
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        await self.attempt_recovery()
        current_state = self.state
        if current_state == RagRuntimeState.DEGRADED:
            _logger.info("CanonicalRagPipeline: DEGRADED state, delegating index_resource to degraded pipeline")
            return await self._get_degraded_pipeline().index_resource(module_id, resource_id, content, metadata, embedding)
        if current_state != RagRuntimeState.CANONICAL:
            raise RuntimeError(f"RAG pipeline not ready for indexing (state={current_state.value})")

        # Canonical path
        try:
            return await self._canonical_index_resource(
                module_id, resource_id, content, metadata, embedding
            )
        except Exception as exc:
            _logger.error("CanonicalRagPipeline: canonical index_resource failed: %s", exc)
            self.state_machine.report_canonical_failure(str(exc))
            return await self._get_degraded_pipeline().index_resource(module_id, resource_id, content, metadata, embedding)

    async def _canonical_index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> IndexState:
        """Canonical index_resource: Qdrant write, then index_state writeback."""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        point_id = str(uuid.uuid4())
        now_utc = datetime.now(timezone.utc).isoformat()
        # Payload contract: opaque ids + filterable metadata only — content
        # and physical locators live in PostgreSQL, never in Qdrant.
        point = PointStruct(
            id=point_id,
            vector=embedding,
            payload=sanitize_payload(
                {
                    "resource_id": resource_id,
                    "module_id": module_id,
                    "content_hash": content_hash,
                    "indexed_at_utc": now_utc,
                    **metadata,
                }
            ),
        )
        await self.qdrant.upsert_points([point])
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
        """Query through the canonical or degraded RAG path depending on state."""
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        await self.attempt_recovery()
        current_state = self.state
        if current_state == RagRuntimeState.DEGRADED:
            _logger.info("CanonicalRagPipeline: DEGRADED state, delegating query to degraded pipeline")
            return await self._get_degraded_pipeline().query(query_embedding, module_id, top_k, score_threshold)
        if current_state != RagRuntimeState.CANONICAL:
            raise RuntimeError(f"RAG pipeline not ready for query (state={current_state.value})")

        # Canonical path
        try:
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
            pg_chunks = await self.postgresql.fetch_chunks_for_points(
                (module_id,) if module_id else tuple(
                    str(h.get("payload", {}).get("module_id") or "")
                    for h in qdrant_hits
                ),
                [str(h.get("id")) for h in qdrant_hits],
            )

            # Step 4: Build typed results via Python domain model
            return self.domain_model.build_typed_result(
                qdrant_hits, pg_metadata, index_states, pg_chunks=pg_chunks
            )
        except Exception as exc:
            _logger.error("CanonicalRagPipeline: canonical query failed: %s", exc)
            # Transition to degraded mode
            self.state_machine.report_canonical_failure(str(exc))
            # Retry with degraded pipeline
            return await self._get_degraded_pipeline().query(query_embedding, module_id, top_k, score_threshold)

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
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        await self.attempt_recovery()
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
        # Canonical read barrier: one batch PG lookup proves every hit —
        # chunk metadata exists, resource is not tombstoned, module scope is
        # in the governed request scope — and hydrates content from the PG
        # authority (Qdrant payloads never carry content).
        chunk_rows = await self.postgresql.fetch_chunks_for_points(
            tuple(module_ids), [str(hit.get("id")) for hit in hits]
        )
        scope = {str(mid) for mid in module_ids}
        proved: list[dict[str, Any]] = []
        for hit in hits:
            payload = hit.get("payload") or {}
            module_id = str(payload.get("module_id") or "")
            document_resource_id = str(
                payload.get("document_resource_id") or payload.get("resource_id") or ""
            )
            if not module_id or module_id not in scope or not document_resource_id:
                continue
            row = chunk_rows.get(str(hit.get("id")))
            if row is None:
                continue  # no canonical chunk metadata / tombstoned
            if str(row.get("resource_id")) != document_resource_id:
                continue  # canonical metadata conflicts with the vector hit
            state = await self.postgresql.get_index_state(module_id, document_resource_id)
            if state is None or str(state.status).lower() not in ("indexed", "active"):
                continue
            record = {
                **payload,
                **{key: value for key, value in row.items() if key != "point_id"},
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
        await self.attempt_recovery()
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        return await self.postgresql.keyword_search(
            query, module_ids=module_ids, limit=limit
        )

    # ------------------------------------------------------------------
    # A52: Four sub-architecture retrievers — share Qdrant + PostgreSQL +
    # embedding runtime + reranker + governance, but differ in retrieval
    # behavior.  The reranker is injected (A49: pipeline does not own a
    # model load).
    # ------------------------------------------------------------------

    def hybrid_retriever(self, reranker=None):
        """A52 hybrid-rag: Qdrant Dense + PG FTS + RRF."""
        from .retrievers import HybridRetriever
        return HybridRetriever(self, reranker)

    def code_retriever(self, reranker=None):
        """A52 code-rag: chunk + AST + symbol + dependency."""
        from .retrievers import CodeRetriever
        return CodeRetriever(self, reranker)

    def agentic_retriever(self, reranker=None, reformulator=None):
        """A52 agentic-rag: retrieve → evaluate → reformulate → retrieve."""
        from .retrievers import AgenticRetriever
        return AgenticRetriever(self, reranker, reformulator)

    def memory_retriever(self, reranker=None):
        """A52 memory-rag: session + episodic + long-term."""
        from .retrievers import MemoryRetriever
        return MemoryRetriever(self, reranker)


__all__ = [
    "CanonicalRagPipeline",
    "RagPipelineConfig",
    "IndexState",
    "RagQueryResult",
    "QdrantCanonicalRuntime",
    "PostgreSQLMetadataAuthority",
    "PythonDomainModel",
    "DegradedRagPipeline",
]