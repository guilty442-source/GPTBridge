"""RAG Runtime Integration — lands the DAG+CAG+RAG hybrid stack.

The governed retrieval stack (A52 four sub-architectures, A371-A374
canonical takeover) existed as tested components but was never
instantiated: ``CAGIntegration`` reads ``app.rag_orchestrator`` and
recorded ``RAG orchestrator not initialized`` on every startup.

This integration is the composition root for the live runtime:

    CanonicalRagPipeline   Qdrant dense + PostgreSQL FTS/authority
      -> formal retrievers retrieve_hybrid/code/memory/metadata
      -> RagOrchestrator   plan -> dispatch -> fuse -> agentic rounds (DAG)
      -> RagApplicationService   capability + two-point sovereign + audit
      -> app.rag_orchestrator    consumed by CAGIntegration
      -> CAGIntegration          -> HybridOrchestrator (CAG/DAG/RAG select)

Startup posture: ``start()`` wires the surfaces synchronously and runs
``pipeline.initialize()`` on the worker loop in the background so the
startup SLA and ``/health`` are not gated on store handshakes.  Retriever
calls during ``STARTING`` wait bounded on the init future; a blocked
canonical contract is logged by the init-done callback and surfaces as
``DEGRADED``/blocked state — never silently canonical.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .rag.embeddings import create_embedding_provider_from_env
from .rag.orchestration.evidence import (
    EvidenceKind,
    RagArchitecture,
    RagEvidence,
    SourceAuthority,
)
from .rag.orchestration.orchestrator import RagOrchestrator
from .rag.pipeline import CanonicalRagPipeline
from .rag.pipeline_retrieval import reciprocal_rank_fusion
from .rag.rag_qdrant import RagPipelineConfig
from .rag.retrievers.code import CodeRetrievalRequest, CodeRetriever
from .rag.retrievers.hybrid import HybridRetrievalRequest, HybridRetriever
from .rag.retrievers.memory import MemoryRetrievalRequest, MemoryRetriever
from .rag.service.authorization import StaticPolicySovereign
from .rag.service.service_api import RagApplicationService

_logger = logging.getLogger("gptbridge.rag.runtime")

# Internal actors granted the governed query surface.  Admission still
# gates module scope at point 1; unknown actors fail closed.
_ACTOR_MODULE_GRANTS = {
    "main-system": frozenset({"*"}),
    "governance/main-system": frozenset({"*"}),
    "xingcheng": frozenset({"*"}),
    "xingcheng-assistant": frozenset({"*"}),
}

_ACTOR_ROLES = {
    "main-system": "admin",
    "governance/main-system": "admin",
    "xingcheng": "xingcheng",
    "xingcheng-assistant": "agent",
    "model-dialogue": "ui",
}


class _RagLoop:
    """Dedicated SelectorEventLoop worker thread.

    ``psycopg.AsyncConnection`` refuses to run on the Windows
    ProactorEventLoop the backend uses by default.  Rather than
    switching the whole process to the selector loop (which would break
    subprocess support), the RAG runtime pins all of its async work —
    pipeline init, retrieval channels, embedding — to one dedicated
    selector loop on a daemon thread.
    """

    def __init__(self) -> None:
        factory = getattr(
            asyncio, "SelectorEventLoop", asyncio.new_event_loop
        )
        self._loop = factory()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._serve, name="rag-runtime-loop", daemon=True
        )

    def _serve(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5)

    def run(self, coro: Any, timeout: Optional[float] = None) -> Any:
        return asyncio.run_coroutine_threadsafe(
            coro, self._loop
        ).result(timeout)

    def submit(self, coro: Any) -> "concurrent.futures.Future[Any]":
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self) -> None:
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
        except Exception:
            pass


async def _hybrid_search_async(
    pipeline: CanonicalRagPipeline,
    query_embedding: list[float],
    query_text: str,
    *,
    module_ids: tuple[str, ...],
    candidate_limit: int,
    score_threshold: Optional[float],
) -> list[dict[str, Any]]:
    """Dense + FTS + RRF inside the worker loop — the psycopg
    connection is affine to this loop, so the channels must await
    here rather than through the mixin's ad-hoc-loop ``_resolve``."""
    vector_hits = await pipeline.vector_search(
        query_embedding,
        module_ids=module_ids,
        top_k=candidate_limit,
        score_threshold=score_threshold,
    )
    keyword_hits = await pipeline.keyword_search(
        query_text, module_ids=module_ids, limit=candidate_limit
    )
    return reciprocal_rank_fusion(vector_hits, keyword_hits)


async def _hybrid_search_reranked_async(
    pipeline: CanonicalRagPipeline,
    query_embedding: list[float],
    query_text: str,
    *,
    module_ids: tuple[str, ...],
    candidate_limit: int,
    top_k: int,
    score_threshold: Optional[float],
    reranker: Any,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    fused = await _hybrid_search_async(
        pipeline,
        query_embedding,
        query_text,
        module_ids=module_ids,
        candidate_limit=candidate_limit,
        score_threshold=score_threshold,
    )
    if not fused:
        return [], {
            "reranker_applied": False,
            "reason": "no-candidates",
            "fallback": "rrf-hybrid-ranking",
        }
    if reranker is None:
        return fused[:top_k], {
            "reranker_applied": False,
            "reason": "no-reranker-injected",
            "fallback": "rrf-hybrid-ranking",
        }
    try:
        from .rag.observability import timed_stage
        with timed_stage("reranker"):
            ranked, meta = reranker(query_text, fused[:candidate_limit])
    except Exception as exc:
        return fused[:top_k], {
            "reranker_applied": False,
            "reason": str(exc),
            "fallback": "rrf-hybrid-ranking",
        }
    return ranked[:top_k], meta


class _SyncRetrievalSurface:
    """Synchronous view over the pipeline's async retrieval channels.

    ``CodeRetriever`` / ``MemoryRetriever`` invoke ``vector_search`` and
    ``keyword_search`` as synchronous calls; on the real
    ``CanonicalRagPipeline`` those are coroutines bound to the worker
    loop.  Every channel is submitted to that loop so the psycopg
    connection never crosses event loops.
    """

    def __init__(
        self,
        pipeline: CanonicalRagPipeline,
        worker: _RagLoop,
        init_future: "concurrent.futures.Future[Any] | None" = None,
    ) -> None:
        self._pipeline = pipeline
        self._worker = worker
        self._init_future = init_future

    def _await_init(self) -> None:
        # During STARTING a retrieval waits bounded on pipeline init so
        # early queries land on the real stores; a failed/absent init
        # falls through to the pipeline's own not-ready behaviour.
        future = self._init_future
        if future is not None and not future.done():
            try:
                future.result(timeout=15)
            except Exception:
                pass

    def vector_search(self, query_embedding, **kwargs):
        self._await_init()
        return self._worker.run(
            self._pipeline.vector_search(query_embedding, **kwargs)
        )

    def keyword_search(self, query, **kwargs):
        self._await_init()
        return self._worker.run(
            self._pipeline.keyword_search(query, **kwargs)
        )

    def hybrid_search(
        self, query_embedding, query_text, **kwargs
    ):
        self._await_init()
        return self._worker.run(
            _hybrid_search_async(
                self._pipeline,
                query_embedding,
                query_text,
                module_ids=tuple(kwargs.get("module_ids") or ()),
                candidate_limit=int(kwargs.get("candidate_limit") or 24),
                score_threshold=kwargs.get("score_threshold"),
            )
        )

    def hybrid_search_reranked(
        self, query_embedding, query_text, **kwargs
    ):
        self._await_init()
        return self._worker.run(
            _hybrid_search_reranked_async(
                self._pipeline,
                query_embedding,
                query_text,
                module_ids=tuple(kwargs.get("module_ids") or ()),
                candidate_limit=int(kwargs.get("candidate_limit") or 24),
                top_k=int(kwargs.get("top_k") or 6),
                score_threshold=kwargs.get("score_threshold"),
                reranker=kwargs.get("reranker"),
            )
        )


def _scope_modules(scope: dict[str, Any]) -> tuple[str, ...]:
    raw = scope.get("module_ids") or ()
    return tuple(str(m) for m in raw if str(m).strip())


def _candidate_to_evidence(
    candidate: dict[str, Any],
    arch: RagArchitecture,
    canonical: bool,
) -> RagEvidence:
    """Map a fused retrieval record onto the formal evidence contract."""
    content = str(
        candidate.get("content")
        or candidate.get("chunk_text")
        or candidate.get("text")
        or ""
    )
    chunk_id = str(
        candidate.get("chunk_id")
        or candidate.get("point_id")
        or candidate.get("id")
        or ""
    )
    evidence_id = (
        f"{arch.value}:{chunk_id}"
        if chunk_id
        else f"{arch.value}:{hashlib.sha256(content.encode()).hexdigest()[:12]}"
    )
    kind = EvidenceKind.SOURCE_TEXT
    if arch is RagArchitecture.CODE:
        kind = EvidenceKind.CODE_SNIPPET
    elif arch is RagArchitecture.MEMORY:
        kind = EvidenceKind.MEMORY
    provenance = {
        key: value for key, value in candidate.items() if key != "content"
    }
    return RagEvidence(
        evidence_id=evidence_id,
        rag_type=arch,
        module_id=str(candidate.get("module_id") or ""),
        resource_id=str(
            candidate.get("document_resource_id")
            or candidate.get("resource_id")
            or ""
        ),
        chunk_id=chunk_id,
        evidence_kind=kind,
        dense_score=float(
            candidate.get("vector_score") or candidate.get("score") or 0.0
        ),
        sparse_score=float(candidate.get("keyword_score") or 0.0),
        reranker_score=float(candidate.get("rrf_score") or 0.0),
        canonical=canonical,
        authority=(
            SourceAuthority.CANONICAL_SOURCE
            if canonical
            else SourceAuthority.DEGRADED_CACHE
        ),
        content=content,
        provenance=provenance,
    )


def _to_evidence(
    candidates: list[dict[str, Any]],
    arch: RagArchitecture,
    canonical: bool,
) -> list[RagEvidence]:
    return [_candidate_to_evidence(c, arch, canonical) for c in candidates]


def _build_retrievers(
    surface: _SyncRetrievalSurface,
    embed_query: Callable[[str], list[float]],
    is_canonical: Callable[[], bool],
) -> dict[str, Callable[[str, dict[str, Any]], list[RagEvidence]]]:
    """Build the four formal ToolFns the orchestrator may dispatch."""

    def _retrieve_hybrid(query: str, scope: dict[str, Any]) -> list[RagEvidence]:
        try:
            embedding = embed_query(query)
            result = HybridRetriever(surface).retrieve(
                HybridRetrievalRequest(
                    query_text=query,
                    query_embedding=embedding,
                    module_ids=_scope_modules(scope),
                )
            )
            return _to_evidence(
                result.candidates, RagArchitecture.HYBRID, is_canonical()
            )
        except Exception as exc:  # noqa: BLE001 — bounded channel
            _logger.warning("retrieve_hybrid failed: %s", exc)
            return []

    def _retrieve_code(query: str, scope: dict[str, Any]) -> list[RagEvidence]:
        try:
            embedding = embed_query(query)
            result = CodeRetriever(surface).retrieve(
                CodeRetrievalRequest(
                    query_text=query,
                    query_embedding=embedding,
                    module_ids=_scope_modules(scope),
                )
            )
            return _to_evidence(
                result.candidates, RagArchitecture.CODE, is_canonical()
            )
        except Exception as exc:  # noqa: BLE001 — bounded channel
            _logger.warning("retrieve_code failed: %s", exc)
            return []

    def _retrieve_memory(query: str, scope: dict[str, Any]) -> list[RagEvidence]:
        try:
            embedding = embed_query(query)
            result = MemoryRetriever(surface).retrieve(
                MemoryRetrievalRequest(
                    query_text=query,
                    query_embedding=embedding,
                    module_ids=_scope_modules(scope),
                    session_id=str(scope.get("session_id") or ""),
                )
            )
            return _to_evidence(
                result.candidates, RagArchitecture.MEMORY, is_canonical()
            )
        except Exception as exc:  # noqa: BLE001 — bounded channel
            _logger.warning("retrieve_memory failed: %s", exc)
            return []

    def _retrieve_metadata(query: str, scope: dict[str, Any]) -> list[RagEvidence]:
        try:
            rows = surface.keyword_search(
                query,
                module_ids=_scope_modules(scope),
                limit=24,
            )
            return _to_evidence(
                list(rows or []), RagArchitecture.HYBRID, is_canonical()
            )
        except Exception as exc:  # noqa: BLE001 — bounded channel
            _logger.warning("retrieve_metadata failed: %s", exc)
            return []

    return {
        "retrieve_hybrid": _retrieve_hybrid,
        "retrieve_code": _retrieve_code,
        "retrieve_memory": _retrieve_memory,
        "retrieve_metadata": _retrieve_metadata,
    }


class _PostgresContentResolver:
    """ContentResolver for ``document`` resources whose content
    authority is the canonical store itself.

    PostgreSQL holds the resource row (``locator_id``) and chunk bodies
    (``chunk.metadata->>'content'``).  This resolver covers re-index /
    rebuild / repair of already-ingested documents; a module that owns
    content elsewhere registers its own resolver for the same or other
    resource types on ``app.rag_content_resolvers``.  Anything
    unresolvable fails closed with ``PermissionError``.
    """

    resource_type = "document"

    def __init__(self, integration: "RagRuntimeIntegration") -> None:
        self._integration = integration

    def resolve(
        self,
        actor_id: str,
        module_id: str,
        locator_id: str,
        version: int,
    ) -> Any:
        from .rag.service.content_resolver import ResolvedContent

        pipeline = self._integration._pipeline
        worker = self._integration._loop_worker
        if pipeline is None or worker is None:
            raise PermissionError("content authority unavailable")
        try:
            row = worker.run(
                pipeline.postgresql.fetch_resource_by_locator(
                    module_id, locator_id
                ),
                timeout=30,
            )
        except Exception as exc:
            raise PermissionError(
                f"content resolution denied: {type(exc).__name__}"
            ) from exc
        if row is None:
            raise PermissionError(
                f"content not found for {module_id}:{locator_id}"
            )
        resource_id = str(row["resource_id"])
        try:
            chunks = worker.run(
                pipeline.postgresql.fetch_resource_chunks(
                    module_id, resource_id
                ),
                timeout=30,
            )
        except Exception as exc:
            raise PermissionError(
                f"content fetch denied: {type(exc).__name__}"
            ) from exc
        content = "\n\n".join(
            str(c.get("content") or "") for c in chunks
        ).strip()
        if not content:
            raise PermissionError(
                f"no stored content for {module_id}:{locator_id}"
            )
        metadata = row.get("metadata") or {}
        return ResolvedContent(
            locator_id=locator_id,
            content=content,
            content_hash=str(row.get("content_hash") or ""),
            version=int(row.get("version") or version or 1),
            resolver="postgresql-content-authority",
            authorized=True,
            provenance={
                "authority": "postgresql",
                "resource_id": resource_id,
                "module_id": module_id,
                "title": str(metadata.get("title") or ""),
            },
        )


@dataclass
class RagRuntimeIntegration:
    """Owns pipeline + orchestrator + governed service lifecycle."""

    app: Any
    _pipeline: Optional[CanonicalRagPipeline] = None
    _orchestrator: Optional[RagOrchestrator] = None
    _service: Optional[RagApplicationService] = None
    _embedder: Any = None
    _loop_worker: Optional[_RagLoop] = None
    _init_future: "concurrent.futures.Future[Any] | None" = None
    _maintenance_registered: bool = False
    _maintenance_task: "concurrent.futures.Future[Any] | None" = None
    _last_parity_at: float = 0.0
    # P0 canonical maintenance cadence — the automation-flows manifest
    # owns the interval when the core drives; this only seeds the
    # no-core fallback loop.
    _MAINTENANCE_INTERVAL_S = float(
        os.environ.get("RAG_MAINTENANCE_INTERVAL_S", "300")
    )
    _started: bool = False
    _dag_planner: Any = None
    _cag_store: Any = None
    _content_resolvers: Any = None
    _query_service: Any = None

    async def start(self) -> dict[str, Any]:
        """Create and initialize the canonical RAG runtime."""
        if self._started:
            return {"ok": True, "already_started": True}

        start_time = time.monotonic()
        dsn = os.environ.get("GPTBRIDGE_POSTGRES_DSN", "").strip()
        if not dsn:
            return {"ok": False, "reason": "GPTBRIDGE_POSTGRES_DSN not set"}

        data_dir = Path(self.app.project_root) / "main-system" / "data" / "rag"
        data_dir.mkdir(parents=True, exist_ok=True)
        config = RagPipelineConfig(
            qdrant_url=os.environ.get(
                "QDRANT_URL", "http://localhost:6333"
            ),
            qdrant_api_key=os.environ.get("QDRANT_API_KEY") or None,
            collection_name=os.environ.get(
                "QDRANT_COLLECTION", "gptbridge_shared_knowledge"
            ),
            postgresql_dsn=dsn,
            queue_db_path=str(data_dir / "reconcile-queue.sqlite3"),
            degraded_root=str(data_dir / "degraded"),
        )

        self._loop_worker = _RagLoop()
        self._loop_worker.start()

        async def _create_embedder() -> Any:
            # Constructed inside the worker loop so the httpx client
            # binds to that loop, matching every embed call site.
            return create_embedding_provider_from_env()

        main_loop = asyncio.get_running_loop()

        def _await_on_worker(coro: Any, timeout: Optional[float]) -> Any:
            # run_coroutine_threadsafe().result() blocks the calling
            # thread — offload the wait so the backend event loop stays
            # responsive while the worker loop does the real work.
            return self._loop_worker.run(coro, timeout)

        self._embedder = await main_loop.run_in_executor(
            None, _await_on_worker, _create_embedder(), 30
        )
        self._pipeline = CanonicalRagPipeline(
            config, embed_texts=self._embedder.embed
        )
        # Pipeline init (store handshakes, schema verification) runs on the
        # worker loop in the background — startup and /health are not gated
        # on it.  The terminal state is logged by the done-callback.
        self._init_future = self._loop_worker.submit(self._pipeline.initialize())
        self._init_future.add_done_callback(self._on_pipeline_init_done)
        self._start_maintenance_driver()
        state = self._pipeline.state.value

        surface = _SyncRetrievalSurface(
            self._pipeline, self._loop_worker, self._init_future
        )

        def _embed_query(query: str) -> list[float]:
            vectors = self._loop_worker.run(
                self._embedder.embed([query]), timeout=30
            )
            return list(vectors[0]) if vectors else []

        def _is_canonical() -> bool:
            return (
                self._pipeline is not None
                and self._pipeline.state.value == "CANONICAL"
                and not self._pipeline.blocked_reason
            )

        # G50: wire the governed local reranker at the orchestrator's
        # post-fusion evidence surface only — injecting it into
        # HybridRetriever too would rerank the same candidates twice.
        # Construction is lazy; the model only loads (through the
        # resource gate) on first use and every failure falls back to
        # fused order.
        from .rag.reranker import LocalCrossEncoderReranker

        reranker = LocalCrossEncoderReranker()
        retrievers = _build_retrievers(surface, _embed_query, _is_canonical)
        self._orchestrator = RagOrchestrator(
            retrievers, reranker=reranker.rerank_evidence
        )

        def _audit_sink(record: Any) -> None:
            try:
                self.app._log({"type": "rag_audit", **asdict(record)})
            except Exception:
                pass

        # A549 DAG production surfaces: the planner is shared (stateless);
        # executors are built per-request because node handlers bind the
        # per-request query/scope/command.
        from .rag.cag import CagCacheStore
        from .rag.dag import RagDagPlanner
        from .rag.query_service import RagQueryService
        from .rag.service.content_resolver import ContentResolverRegistry

        self._dag_planner = RagDagPlanner()
        self._cag_store = CagCacheStore()
        self._content_resolvers = ContentResolverRegistry()
        # 'document' content resolves against the canonical store itself —
        # PostgreSQL is the content authority (chunk.metadata.content);
        # owning modules may still override by registering their own
        # resolver on app.rag_content_resolvers.
        self._content_resolvers.register(
            _PostgresContentResolver(lambda: self._pipeline)
        )

        self._service = RagApplicationService(
            self._orchestrator,
            StaticPolicySovereign(
                module_grants=_ACTOR_MODULE_GRANTS,
                default_resource_allow=True,
            ),
            actor_role_of=lambda actor: _ACTOR_ROLES.get(actor, "agent"),
            audit_sink=_audit_sink,
            dag_planner=self._dag_planner,
            index_executor_factory=self._index_executor_factory,
            repair_executor_factory=self._repair_executor_factory,
        )

        self._query_service = RagQueryService(
            planner=self._dag_planner,
            store=self._cag_store,
            executor_factory=self._query_executor_factory,
        )

        self.app.rag_pipeline = self._pipeline
        self.app.rag_orchestrator = self._orchestrator
        self.app.rag_service = self._service
        self.app.rag_query_service = self._query_service
        self.app.rag_content_resolvers = self._content_resolvers
        self._started = True

        return {
            "ok": True,
            "started_at": time.time(),
            "duration_ms": int((time.monotonic() - start_time) * 1000),
            "pipeline_initialized": False,
            "background_init": True,
            "state": state,
            "blocked_reason": "",
        }

    # ------------------------------------------------------------------
    # A549 DAG facades — per-request executor factories
    # ------------------------------------------------------------------

    def _dag_generate(self, context_text: str) -> Mapping[str, Any]:
        """MODEL_INFERENCE binding: run the governed native model on the
        RAG worker loop (the coroutine is the real inference path; the
        DAG executor thread simply waits on it)."""
        from .xingcheng_native_model_runtime import native_model_infer

        try:
            return self._loop_worker.run(
                native_model_infer(
                    context_text,
                    intent="rag-answer",
                    grounding=context_text,
                    max_tokens=256,
                ),
                timeout=110,
            )
        except Exception as exc:
            return {
                "ok": False,
                "error_code": "GENERATE_FAILED",
                "message": str(exc),
            }

    def _query_executor_factory(
        self,
        cache_request: Any,
        plan_request: Any,
        dag_context: Any,
    ) -> Any:
        from .rag.dag import RagDagExecutor
        from .rag.dag.handlers import query_chain_handlers

        return RagDagExecutor(
            query_chain_handlers(
                self._orchestrator,
                query=cache_request.query,
                scope={"module_ids": dag_context.module_ids},
                cache_store=self._cag_store,
                cache_request=cache_request,
                generate=self._dag_generate,
                policy_version="a549-query",
            ),
            node_timeout_seconds=120.0,
        )

    def _index_executor_factory(self, command: Any) -> Any:
        from .rag.dag import RagDagExecutor
        from .rag.dag.handlers import write_path_handlers

        return RagDagExecutor(
            write_path_handlers(
                index_fn=lambda **kw: self._dag_index(command),
                verify_fn=lambda target: self._dag_verify(command),
                publish_fn=lambda target, verification: self._dag_verify(command),
            ),
            node_timeout_seconds=90.0,
        )

    def _dag_index(self, command: Any) -> dict[str, Any]:
        """INDEX node body: resolve -> chunk -> embed -> pipeline write."""
        try:
            resolved = self._content_resolvers.resolve(
                command.actor_id,
                command.module_id,
                command.resource_type,
                command.locator_id,
                version=0,
            )
        except PermissionError as exc:
            return {"ok": False, "error": f"index:content-resolver:{exc}"}
        if not resolved.authorized:
            return {"ok": False, "error": "index:content-not-authorized"}
        if not str(resolved.content or "").strip():
            return {"ok": False, "error": "index:empty-content"}

        from .rag.chunking import Document, create_chunking_service_from_env

        document = Document(
            resource_id=command.resource_id,
            module_id=command.module_id,
            content=resolved.content,
            metadata={
                "locator_id": command.locator_id,
                "data_category": command.data_category,
            },
        )
        chunks = create_chunking_service_from_env().chunk_document(document)
        if not chunks:
            return {"ok": False, "error": "index:no-chunks"}

        indexed = 0
        last_hash = ""
        for chunk in chunks:
            vectors = self._loop_worker.run(
                self._embedder.embed([chunk.content]), timeout=60
            )
            embedding = list(vectors[0]) if vectors else []
            if not embedding:
                return {"ok": False, "error": "index:empty-embedding"}
            state = self._loop_worker.run(
                self._pipeline.index_resource(
                    command.module_id,
                    command.resource_id,
                    chunk.content,
                    metadata={
                        **chunk.metadata,
                        "chunk_index": chunk.index,
                        "locator_id": command.locator_id,
                        "data_category": command.data_category,
                    },
                    embedding=embedding,
                ),
                timeout=60,
            )
            last_hash = getattr(state, "content_hash", "") or last_hash
            indexed += 1
        return {
            "ok": True,
            "indexed_points": indexed,
            "source_revision": str(resolved.version or ""),
            "content_hash": last_hash,
        }

    def _dag_verify(self, command: Any) -> dict[str, Any]:
        """VERIFICATION / PUBLISH_BARRIER body: independent read-back of
        the authoritative index_state (PostgreSQL is the authority)."""
        try:
            state = self._loop_worker.run(
                self._pipeline.get_index_state(
                    command.module_id, command.resource_id
                ),
                timeout=30,
            )
        except Exception as exc:
            return {"ok": False, "error": f"verify:index-state:{exc}"}
        visible = state is not None
        return {
            "ok": visible,
            "verification_result": "verified" if visible else "missing",
            "independent_verifier": "postgresql-index-state",
            "publish_state": "published" if visible else "not-visible",
            "authority_marker": getattr(state, "content_hash", "") or "",
        }

    # ------------------------------------------------------------------
    # P0: governed canonical maintenance driver
    # ------------------------------------------------------------------

    def _parity_due(self) -> bool:
        pipeline = self._pipeline
        interval = getattr(
            pipeline, "_PARITY_SWEEP_INTERVAL_S", 300.0
        ) if pipeline is not None else 300.0
        now = time.monotonic()
        if now - self._last_parity_at < interval:
            return False
        self._last_parity_at = now
        return True

    async def _maintenance_tick(self) -> None:
        """One cycle on the RAG worker loop — called by the automation
        core or the private fallback loop."""
        pipeline, worker = self._pipeline, self._loop_worker
        if pipeline is None or worker is None or not self._started:
            return
        await asyncio.to_thread(
            worker.run,
            pipeline.run_maintenance_cycle(
                include_parity=self._parity_due()
            ),
            pipeline._MAINTENANCE_BUDGET_S + 30,
        )

    def _start_maintenance_driver(self) -> None:
        """Register the canonical maintenance cycle.

        Automation core owns the cadence when present (kill-switchable
        through automation-flows.json; a denied registration must NOT
        fall back to a private loop).  Without a core the worker loop
        runs its own recurring task — private-loop fallback is only
        legitimate with no core at all.
        """
        core = getattr(self.app, "automation_core", None)
        if core is not None:
            self._maintenance_registered = bool(
                core.register_flow("rag-maintenance", self._maintenance_tick)
            )
            return
        if self._loop_worker is not None:
            self._maintenance_task = self._loop_worker.submit(
                self._private_maintenance_loop()
            )

    async def _private_maintenance_loop(self) -> None:
        while self._started:
            try:
                await asyncio.sleep(self._MAINTENANCE_INTERVAL_S)
                if not self._started:
                    break
                pipeline = self._pipeline
                if pipeline is not None:
                    await pipeline.run_maintenance_cycle(
                        include_parity=self._parity_due()
                    )
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 — maintenance never kills the loop
                continue

    def _on_pipeline_init_done(
        self, future: "concurrent.futures.Future[Any]"
    ) -> None:
        """Log the terminal init state — runs on the worker thread."""
        error = ""
        initialized = False
        try:
            initialized = bool(future.result())
        except Exception as exc:  # init coroutine itself raised
            error = f"{type(exc).__name__}: {exc}"
        # P0 resume: one immediate maintenance pass after init so outbox
        # work orphaned by the previous process lifetime is reclaimed
        # and drained before the first periodic tick.
        if initialized and not error:
            pipeline, worker = self._pipeline, self._loop_worker
            if pipeline is not None and worker is not None:
                try:
                    worker.submit(pipeline.run_maintenance_cycle())
                except Exception:
                    pass
        state = self._pipeline.state.value if self._pipeline else "absent"
        blocked = (
            self._pipeline.blocked_reason if self._pipeline else None
        )
        try:
            self.app._log(
                {
                    "type": "status",
                    "message": "RAG pipeline initialized",
                    "ok": initialized and not error,
                    "pipeline_initialized": initialized,
                    "state": state,
                    "blocked_reason": blocked or "",
                    "init_error": error,
                }
            )
        except Exception:
            pass

    def wait_initialized(self, timeout: Optional[float] = None) -> bool:
        """Block the calling thread until pipeline init settles."""
        future = self._init_future
        if future is None:
            return False
        try:
            return bool(future.result(timeout=timeout))
        except Exception:
            return False

    async def wait_initialized_async(
        self, timeout: Optional[float] = None
    ) -> bool:
        return await asyncio.to_thread(self.wait_initialized, timeout)

    async def stop(self) -> dict[str, Any]:
        """Detach the runtime surfaces (called after CAG stop)."""
        if not self._started:
            return {"ok": True, "already_stopped": True}
        stop_start = time.monotonic()
        future = self._init_future
        if future is not None and not future.done():
            future.cancel()
        # Release the maintenance driver before teardown — no in-flight
        # tick may re-enter a stopped pipeline.
        core = getattr(self.app, "automation_core", None)
        if core is not None and self._maintenance_registered:
            try:
                core.unregister("rag-maintenance")
            except Exception:
                pass
        self._maintenance_registered = False
        task = self._maintenance_task
        if task is not None and not task.done():
            task.cancel()
        self._maintenance_task = None
        for attr in (
            "rag_service",
            "rag_orchestrator",
            "rag_pipeline",
            "rag_query_service",
            "rag_content_resolvers",
        ):
            if getattr(self.app, attr, None) is not None:
                setattr(self.app, attr, None)
        worker = self._loop_worker
        if worker is not None:
            try:
                conn = getattr(
                    self._pipeline.postgresql, "_conn", None
                ) if self._pipeline is not None else None
                if conn is not None:
                    worker.run(conn.close(), timeout=10)
                client = getattr(self._embedder, "_client", None)
                if client is not None:
                    worker.run(client.aclose(), timeout=10)
            except Exception:
                pass
            worker.stop()
        self._service = None
        self._query_service = None
        self._orchestrator = None
        self._pipeline = None
        self._embedder = None
        self._dag_planner = None
        self._cag_store = None
        self._content_resolvers = None
        self._loop_worker = None
        self._init_future = None
        self._started = False
        return {
            "ok": True,
            "duration_ms": int((time.monotonic() - stop_start) * 1000),
        }

    def run_schema_migration(
        self,
        *,
        current: Any = None,
        target: Any = None,
        apply_metadata: Any = None,
        validate: Any = None,
        benchmark_fn: Any = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        """G50/P2: governed schema-drift migration entry point.

        Explicit operator/governed-flow call — never invoked
        automatically on a maintenance tick or at init. Vector-axis
        drift builds a new generation through ``rebuild_canonical``
        (PG authority -> validate -> atomic promote). With no explicit
        ``current``/``target`` and no detected fingerprint drift this is
        a no-op — an accidental call can never trigger a rebuild.
        """
        pipeline = self._pipeline
        worker = self._loop_worker
        if not self._started or pipeline is None or worker is None:
            return {"ok": False, "blocked_reason": "rag-runtime-not-started"}
        mgr = getattr(pipeline, "generation_manager", None)
        if mgr is None:
            return {
                "ok": False,
                "blocked_reason": "generation-manager-unavailable",
            }
        drift = bool(
            getattr(pipeline, "_generation_rebuild_required", False)
        )
        if current is None and target is None and not drift:
            return {
                "ok": True,
                "action": "none",
                "reason": "no-drift-detected",
            }
        if current is None or target is None:
            from .rag.lifecycle.schema_versions import RagSchemaVersions

            schema = pipeline.manifest().schema
            current = current or schema
            # 指紋漂移一律走向量軸 → 新世代建置相位。
            target = target or RagSchemaVersions(
                rag_schema_version=schema.rag_schema_version,
                metadata_schema_version=schema.metadata_schema_version,
                vector_schema_version=schema.vector_schema_version + 1,
            )
        report = worker.run(
            pipeline.migrate_schema(
                mgr,
                current=current,
                target=target,
                apply_metadata=apply_metadata,
                validate=validate,
                benchmark_fn=benchmark_fn,
            ),
            timeout=timeout,
        )
        result = {
            "ok": bool(report.ok),
            "blocked_reason": report.blocked_reason,
            "phases_completed": [
                phase.value for phase in report.phases_completed
            ],
            "vector_generation_built": report.vector_generation_built,
            "drift_detected": drift,
            "plan_steps": list(report.plan.steps),
        }
        try:
            self.app._log(
                {
                    "type": "status",
                    "message": "RAG schema migration",
                    "ok": result["ok"],
                    "blocked_reason": result["blocked_reason"],
                    "vector_generation_built": result[
                        "vector_generation_built"
                    ],
                    "drift_detected": drift,
                }
            )
        except Exception:
            pass
        return result

    def get_stats(self) -> dict[str, Any]:
        future = self._init_future
        return {
            "started": self._started,
            "init_done": future.done() if future is not None else False,
            "state": (
                self._pipeline.state.value if self._pipeline else "absent"
            ),
            "gateway_state": (
                self._pipeline.gateway_state() if self._pipeline else ""
            ),
        }


def create_rag_runtime_integration(app: Any) -> RagRuntimeIntegration:
    """Create the RAG runtime integration for the app."""
    return RagRuntimeIntegration(app)


__all__ = [
    "RagRuntimeIntegration",
    "create_rag_runtime_integration",
]
