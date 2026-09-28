"""RAG Pipeline — canonical RAG path (A371-A374) + A44 degraded delegation.

A371/A374: vectord dense retrieval > PostgreSQL metadata/FTS/index_state >
Python domain model > typed result.  A373: canonical read/write must prove
both stores are live; DEGRADED state delegates to ``DegradedRagPipeline``.
"""

from __future__ import annotations

import hashlib
import logging

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .vector_models import PointStruct

_logger = logging.getLogger("gptbridge.rag")


from .canonical_vector_runtime import (
    IndexState,
    CanonicalVectorRuntime,
    RagPipelineConfig,
    RagQueryResult,
    sanitize_payload,
)
from .rust_vector_runtime import select_vector_runtime
from shared_layer.local.pg_adapter import connect as pg_connect
from shared_layer.security.vector_scope import VectorScopeError
from .rag_metadata import PostgreSQLMetadataAuthority
from .pipeline_degraded import DegradedRagPipeline
from .pipeline_documents import PipelineDocumentsMixin
from .pipeline_domain import PythonDomainModel
from .pipeline_maintenance import PipelineMaintenanceMixin
from .pipeline_outbox import PipelineOutboxMixin
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
    PipelineOutboxMixin,
    PipelineMaintenanceMixin,
    PipelineDocumentsMixin,
):
    """A371-A374: canonical path — vectord dense retrieval > PostgreSQL
    metadata/FTS/index_state authority > domain model > typed result.
    A373: normal execution must prove vectord + PostgreSQL are live."""

    def __init__(
        self,
        config: RagPipelineConfig,
        *,
        document_fetcher: Optional[Any] = None,
        embed_texts: Optional[Any] = None,
        vector_runtime: Optional[Any] = None,
    ) -> None:
        self.config = config
        # A610 target-primary: the canonical vector runtime is the Rust
        # vectord engine by default; ``vector_runtime`` stays injectable
        # for governed stand-ins and migration tooling.
        self.vector = vector_runtime or select_vector_runtime(config)
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
        # A374: runtime state machine.  The durable queue lives in the
        # ``gptbridge_rag`` PostgreSQL schema (A610/A621: SQLite retired).
        # The TombstoneGuard is the in-memory authoritative view;
        # PostgreSQL is the durable tombstone store.
        self._queue_db = pg_connect(self.config.queue_schema, autocommit=False)
        self._queue = ReconciliationQueue(self._queue_db)
        self._tombstone = TombstoneGuard()
        self._outbox = CrossStoreOutbox(self._queue, self._tombstone)
        self._state_machine = RagRuntimeStateMachine(
            self._queue, self._tombstone, self._outbox
        )
        # Canonical takeover: sticky hard-contract violation (dimension
        # mismatch, non-loopback vectord URL, unverifiable contract).  While
        # set the surface state is BLOCKED and canonical reads/writes raise
        # instead of silently delegating to the degraded backend.
        self._blocked_reason: Optional[str] = None
        # RAG-11: set while a BUILDING generation / alias migration runs;
        # surfaces as MIGRATING on the gateway state.
        self._migrating = False
        # RAG-16: ACTIVE generation id bound to this pipeline; retrieval
        # drops hits carrying a different generation_id.
        self._active_generation: Optional[str] = None
        # G50: production assembly point for the A486/A487 generation
        # lifecycle.  Constructed during initialize() once vectord +
        # PostgreSQL are proven; ``_generation_rebuild_required`` flags a
        # config-fingerprint drift that demands a NEW generation (never an
        # in-place edit of the ACTIVE one).
        self.generation_manager: Optional[Any] = None
        self._generation_rebuild_required = False

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
        STARTING -> CANONICAL only when vectord + PostgreSQL are healthy,
        the authoritative index_state matches, and the reconciliation queue
        is complete.  Otherwise STARTING -> DEGRADED.
        """
        _logger.info("CanonicalRagPipeline: initializing...")

        # Step 1: vectord canonical runtime
        vector_ok = await self.vector.initialize()
        if vector_ok:
            await self.vector.ensure_collection()
        if self.vector.collection_error or "_URL_NOT_LOOPBACK" in (
            self.vector.last_error or ""
        ):
            # Hard contract violation — BLOCKED, never silently degraded.
            self._blocked_reason = (
                self.vector.collection_error or self.vector.last_error
            )

        # Step 2: PostgreSQL metadata authority
        pg_ok = await self.postgresql.initialize()

        # G50: bind the ACTIVE index generation once both stores are
        # proven — retrieval drops hits from any other generation
        # (RAG-16), and a config-fingerprint drift marks the pipeline
        # as needing a new-generation build before canonical indexing.
        vector_client = getattr(self.vector, "client", None)
        if vector_ok and pg_ok and vector_client is not None:
            try:
                from .generation import GenerationConfig, GenerationManager

                self.generation_manager = GenerationManager(
                    vector_client,
                    GenerationConfig(
                        alias_name=self.config.collection_name,
                        embedding_model=self.config.embedding_model,
                        embedding_dimension=self.config.embedding_dimension,
                        chunk_size=self.config.chunk_size,
                        chunk_overlap=self.config.chunk_overlap,
                    ),
                    self.postgresql,
                )
                active = await self.generation_manager.get_active_generation()
                if active is not None:
                    self._active_generation = str(active.generation_id)
                    self._generation_rebuild_required = (
                        await self.generation_manager.requires_rebuild(active)
                    )
                    if self._generation_rebuild_required:
                        _logger.warning(
                            "CanonicalRagPipeline: active generation %s "
                            "fingerprint drifted from config — new "
                            "generation build required",
                            self._active_generation,
                        )
            except Exception as exc:
                _logger.warning(
                    "CanonicalRagPipeline: generation binding failed: %s", exc
                )

        self._initialized = (
            vector_ok and pg_ok and self._blocked_reason is None
        )
        _logger.info("CanonicalRagPipeline: initialized=%s (vector=%s, pg=%s)", self._initialized, vector_ok, pg_ok)

        # A374: evaluate startup readiness gate.
        index_state_matches = self._initialized  # minimal: both stores up
        if self._generation_rebuild_required:
            # The ACTIVE generation's fingerprint no longer matches the
            # configured embedding/index contract — canonical indexing is
            # stale until a new generation is built/verified/activated.
            index_state_matches = False
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
            vector_healthy=vector_ok,
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
        if self._migrating:
            return "MIGRATING"
        mapping = {
            "STARTING": "BOOTSTRAPPING",
            "CANONICAL": "CANONICAL_READY",
            "DEGRADED": "DEGRADED_READY",
            "RECONCILING": "RECONCILING",
            "RECONCILIATION_FAILED": "DEGRADED_READY",
        }
        return mapping.get(self._state_machine.effective_state, "BOOTSTRAPPING")

    def is_ready(self) -> bool:
        """A373: Prove vectord + PostgreSQL are live path."""
        return (
            self._initialized
            and self._blocked_reason is None
            and self.vector.is_healthy()
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
        """Canonical index_resource: vectord write, then index_state writeback."""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        point_id = str(uuid.uuid4())
        now_utc = datetime.now(timezone.utc).isoformat()
        # Payload contract: opaque ids + filterable metadata only — content
        # and physical locators live in PostgreSQL, never in vectord.
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
        await self.vector.upsert_points([point])
        index_state = IndexState(
            resource_id=resource_id,
            module_id=module_id,
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            indexed_at_utc=now_utc,
            content_hash=content_hash,
            vector_point_id=point_id,
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
        if not str(module_id or "").strip():
            raise VectorScopeError("VECTOR_MODULE_SCOPE_REQUIRED")
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
            # A373: CANONICAL-TAKEOVER - prove vectord + PostgreSQL are live
            if not self.vector.is_healthy():
                raise RuntimeError("vectord canonical runtime not healthy")
            if not self.postgresql.is_healthy():
                raise RuntimeError("PostgreSQL metadata authority not healthy")

            # Step 1: vectord dense retrieval
            vector_hits = await self.vector.search(
                query_vector=query_embedding,
                module_id=module_id,
                top_k=top_k,
                score_threshold=score_threshold,
            )

            if not vector_hits:
                return []

            pg_metadata, index_states = await self._pg_evidence(vector_hits)
            pg_chunks = await self.postgresql.fetch_chunks_for_points(
                (module_id,) if module_id else tuple(
                    str(h.get("payload", {}).get("module_id") or "")
                    for h in vector_hits
                ),
                [str(h.get("id")) for h in vector_hits],
            )

            # Step 4: Build typed results via Python domain model
            return self.domain_model.build_typed_result(
                vector_hits, pg_metadata, index_states, pg_chunks=pg_chunks
            )
        except Exception as exc:
            _logger.error("CanonicalRagPipeline: canonical query failed: %s", exc)
            # Transition to degraded mode
            self.state_machine.report_canonical_failure(str(exc))
            # Retry with degraded pipeline
            return await self._get_degraded_pipeline().query(query_embedding, module_id, top_k, score_threshold)

    async def _pg_evidence(
        self, vector_hits: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Steps 2-3: batch-fetch PostgreSQL metadata + index_state proof."""
        resource_ids = [
            hit.get("payload", {}).get("resource_id") or hit.get("id")
            for hit in vector_hits
        ]
        module_ids = set(
            hit.get("payload", {}).get("module_id")
            for hit in vector_hits
            if hit.get("payload", {}).get("module_id")
        )
        pg_metadata: dict[str, Any] = {}
        index_states: dict[str, Any] = {}
        get_states = getattr(self.postgresql, "get_index_states", None)
        for mid in module_ids:
            pg_metadata.update(await self.postgresql.fetch_metadata(mid, resource_ids))
            if get_states is not None:
                # P15: one round trip per module instead of one per resource.
                for rid, state in (
                    await get_states(mid, resource_ids)
                ).items():
                    index_states[f"{mid}:{rid}"] = state
            else:
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
        from .observability import RAG_METRICS, current_trace, timed_stage
        RAG_METRICS.inc("rag_query_total")
        RAG_METRICS.inc("canonical_query_total")
        trace = current_trace()
        started = time.perf_counter()
        try:
            with timed_stage("vector"):
                hits = await self.vector.search(
                    query_vector=query_embedding,
                    module_ids=module_ids,
                    top_k=top_k,
                    score_threshold=score_threshold,
                )
        except Exception:
            RAG_METRICS.inc("rag_query_failed_total")
            RAG_METRICS.inc("vector_error_total")
            RAG_METRICS.observe_latency(
                (time.perf_counter() - started) * 1000
            )
            raise
        return await self._prove_vector_hits(hits, module_ids, trace, started)

    async def text_search(
        self,
        query_text: str,
        *,
        module_ids: tuple[str, ...],
        top_k: int,
        score_threshold: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Canonical dense retrieval by raw text (PERF-07).

        The owning engine embeds ``query_text`` internally — no vector is
        serialised across the Python↔vectord boundary.  The PostgreSQL
        index_state proof barrier below is identical to
        :meth:`vector_search`.
        """
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        await self.attempt_recovery()
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        from .observability import RAG_METRICS, current_trace, timed_stage
        RAG_METRICS.inc("rag_query_total")
        RAG_METRICS.inc("canonical_query_total")
        trace = current_trace()
        started = time.perf_counter()
        try:
            with timed_stage("vector"):
                hits = await self.vector.search_text(
                    query_text,
                    module_ids=module_ids,
                    top_k=top_k,
                    score_threshold=score_threshold,
                )
        except Exception:
            RAG_METRICS.inc("rag_query_failed_total")
            RAG_METRICS.inc("vector_error_total")
            RAG_METRICS.observe_latency(
                (time.perf_counter() - started) * 1000
            )
            raise
        return await self._prove_vector_hits(hits, module_ids, trace, started)

    async def _prove_vector_hits(
        self,
        hits: list[dict[str, Any]],
        module_ids: tuple[str, ...],
        trace: Any,
        started: float,
    ) -> list[dict[str, Any]]:
        """Shared canonical read barrier for vector/text searches."""
        from .observability import RAG_METRICS, timed_stage
        if not hits:
            RAG_METRICS.inc("retrieval_zero_result_total")
            RAG_METRICS.observe_latency(
                (time.perf_counter() - started) * 1000
            )
            return []
        # Canonical read barrier: one batch PG lookup proves every hit —
        # chunk metadata exists, resource is not tombstoned, module scope is
        # in the governed request scope — and hydrates content from the PG
        # authority (vectord payloads never carry content).
        with timed_stage("postgres_fts"):
            chunk_rows = await self.postgresql.fetch_chunks_for_points(
                tuple(module_ids), [str(hit.get("id")) for hit in hits]
            )
        scope = {str(mid) for mid in module_ids}
        # Batch the index-state proof: one PostgreSQL round trip per module
        # instead of one per hit (P15 N+1 remediation).
        rids_by_module: dict[str, list[str]] = {}
        for hit in hits:
            payload = hit.get("payload") or {}
            mid = str(payload.get("module_id") or "")
            rid = str(
                payload.get("document_resource_id") or payload.get("resource_id") or ""
            )
            if mid and rid:
                rids_by_module.setdefault(mid, []).append(rid)
        index_states: dict[tuple[str, str], Any] = {}
        get_states = getattr(self.postgresql, "get_index_states", None)
        for mid, rids in rids_by_module.items():
            if get_states is not None:
                for rid, state in (await get_states(mid, rids)).items():
                    index_states[(mid, str(rid))] = state
            else:
                for rid in rids:
                    state = await self.postgresql.get_index_state(mid, rid)
                    if state is not None:
                        index_states[(mid, rid)] = state
        proved: list[dict[str, Any]] = []
        for hit in hits:
            payload = hit.get("payload") or {}
            module_id = str(payload.get("module_id") or "")
            document_resource_id = str(
                payload.get("document_resource_id") or payload.get("resource_id") or ""
            )
            if not module_id or module_id not in scope or not document_resource_id:
                if trace is not None:
                    trace.drop("unauthorized")
                RAG_METRICS.inc("unauthorized_hit_dropped_total")
                continue
            if (
                self._active_generation
                and str(payload.get("generation_id") or "")
                not in ("", self._active_generation)
            ):
                if trace is not None:
                    trace.drop("generation_mismatch")
                RAG_METRICS.inc("generation_mismatch_hit_dropped_total")
                continue
            row = chunk_rows.get(str(hit.get("id")))
            if row is None:
                # no canonical chunk metadata / tombstoned
                if trace is not None:
                    trace.drop("missing_metadata")
                RAG_METRICS.inc("missing_metadata_hit_dropped_total")
                continue
            if str(row.get("resource_id")) != document_resource_id:
                if trace is not None:
                    trace.drop("missing_metadata")
                RAG_METRICS.inc("missing_metadata_hit_dropped_total")
                continue  # canonical metadata conflicts with the vector hit
            state = index_states.get((module_id, document_resource_id))
            if state is None or str(state.status).lower() not in ("indexed", "active"):
                if trace is not None:
                    trace.drop("tombstoned")
                RAG_METRICS.inc("tombstoned_hit_dropped_total")
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
        RAG_METRICS.inc("rag_query_success_total")
        RAG_METRICS.observe_latency((time.perf_counter() - started) * 1000)
        if trace is not None:
            trace.hit_count = len(proved)
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
        from .observability import timed_stage
        with timed_stage("postgres_fts"):
            return await self.postgresql.keyword_search(
                query, module_ids=module_ids, limit=limit
            )

    # ------------------------------------------------------------------
    # A52: Four sub-architecture retrievers — share vectord + PostgreSQL +
    # embedding runtime + reranker + governance, but differ in retrieval
    # behavior.  The reranker is injected (A49: pipeline does not own a
    # model load).
    # ------------------------------------------------------------------

    def hybrid_retriever(self, reranker=None):
        """A52 hybrid-rag: vectord Dense + PG FTS + RRF."""
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
    "CanonicalVectorRuntime",
    "PostgreSQLMetadataAuthority",
    "PythonDomainModel",
    "DegradedRagPipeline",
]