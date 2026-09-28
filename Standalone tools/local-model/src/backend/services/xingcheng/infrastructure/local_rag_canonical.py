"""Canonical RAG adapter — sync facade binding LocalRagService to the
CanonicalRagPipeline (A371-A374).

The canonical pipeline is async (psycopg AsyncConnection + vectord, the
Rust vector engine).  This adapter owns a dedicated background event-loop
thread (Windows selector policy, required by psycopg) and exposes bounded
synchronous methods so the sync ``LocalRagService`` can use the live
canonical path:

    query:   query_vector + keyword_search  (vectord dense + PG FTS/index_state)
    ingest:  index_document                 (vectord points + PG authority rows)

When the pipeline is unavailable (services down, optional dependencies
missing, DSN unset) the adapter stays not-ready and every caller falls back
to the bounded degraded local path (A44) — nothing here is ever treated as
the degraded authority itself.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Optional, Sequence

_logger = logging.getLogger("gptbridge.local_rag.canonical")

_DEFAULT_VECTORD_URL = "http://127.0.0.1:8092"
_DEFAULT_COLLECTION = "gptbridge_shared_knowledge"
_CALL_TIMEOUT_SECONDS = 30.0


def _resolve_project_root(tool_root: Path) -> Path:
    env_root = os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT") or os.environ.get(
        "GPTBRIDGE_ROOT"
    )
    if env_root:
        return Path(env_root).resolve()
    # infrastructure/ -> xingcheng -> services -> backend -> src -> local-model
    # -> "Standalone tools" -> <repo root>
    for parent in Path(__file__).resolve().parents:
        if (parent / "main-system" / "src-core").is_dir():
            return parent
    return Path(tool_root).resolve()


def _ensure_import_paths(project_root: Path) -> None:
    for candidate in (
        project_root,
        project_root / "shared-layer" / "src",
        project_root / "main-system" / "src-core",
    ):
        text = str(candidate)
        if candidate.is_dir() and text not in sys.path:
            sys.path.append(text)


class CanonicalRagAdapter:
    """Bounded sync facade over the async CanonicalRagPipeline."""

    def __init__(
        self,
        tool_root: Path,
        native_runtime: Any = None,
        *,
        enabled: bool = True,
        document_fetcher: Any = None,
        embed_texts: Any = None,
    ) -> None:
        self._tool_root = Path(tool_root).resolve()
        self._native_runtime = native_runtime
        self._enabled = enabled
        # A374 reconciliation data flow: the fetcher re-reads the owning
        # module's original content from the degraded mirror; the embedder
        # regenerates native hashed vectors — degraded copies are never
        # replayed into the canonical collection.
        self._document_fetcher = document_fetcher
        self._embed_texts = embed_texts or (
            (lambda texts: native_runtime.embed(texts))
            if native_runtime is not None
            else None
        )
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._pipeline: Any = None
        self._ready = False
        self._last_error: Optional[str] = None
        self._init_event = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def _start(self) -> None:
        with self._lock:
            if self._thread is not None or not self._enabled:
                return
            self._thread = threading.Thread(
                target=self._run, name="canonical-rag-loop", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        try:
            policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
            if policy is not None:
                asyncio.set_event_loop_policy(policy())
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._initialize())
            self._init_event.set()
            self._loop.run_forever()
        except Exception as exc:  # pragma: no cover - defensive loop guard
            self._last_error = str(exc)
            self._init_event.set()

    async def _initialize(self) -> None:
        try:
            _ensure_import_paths(_resolve_project_root(self._tool_root))
            from core_system.rag.pipeline import (
                CanonicalRagPipeline,
                RagPipelineConfig,
            )

            dsn = os.environ.get("GPTBRIDGE_POSTGRES_DSN", "")
            if not dsn:
                self._last_error = "GPTBRIDGE_POSTGRES_DSN_REQUIRED"
                return
            embedding_model = "xingcheng-hashed-embedding-v1"
            runtime = self._native_runtime
            if runtime is not None:
                embedding_model = str(getattr(runtime, "EMBEDDING_MODEL", embedding_model))
            self._pipeline = CanonicalRagPipeline(
                self._pipeline_config(RagPipelineConfig, dsn, embedding_model),
                document_fetcher=self._document_fetcher,
                embed_texts=self._embed_texts,
            )
            self._ready = bool(await self._pipeline.initialize())
            if not self._ready:
                self._last_error = "canonical-initialize-failed"
        except Exception as exc:
            self._last_error = str(exc)
            self._pipeline = None
            self._ready = False

    def _pipeline_config(
        self, config_type: Any, dsn: str, embedding_model: str
    ) -> Any:
        """Governed local contract + durable A374 stores."""
        state_dir = Path(self._tool_root) / "runtime" / "state"
        return config_type(
            collection_name=os.environ.get(
                "VECTOR_COLLECTION", _DEFAULT_COLLECTION
            ),
            postgresql_dsn=dsn,
            vectord_url=os.environ.get("VECTORD_URL", _DEFAULT_VECTORD_URL),
            # 原生 hashed embedding（in-process，無外部模型相依）。
            embedding_model=embedding_model,
            embedding_dimension=int(
                getattr(self._native_runtime, "EMBEDDING_DIMENSION", 1024)
            ),
            embedding_provider="native-hashed",
            chunk_size=1200,
            chunk_overlap=200,
            top_k=48,
            score_threshold=0.0,
            # A374: the durable pending_rag_mutation queue lives in
            # ``gptbridge_rag`` (PostgreSQL); degraded stores stay JSON
            # files that survive restarts between canonical outages.
            queue_db_path="postgresql:gptbridge_rag",
            degraded_root=str(state_dir / "rag-degraded"),
        )

    def _submit(self, coroutine: Any, timeout: float = _CALL_TIMEOUT_SECONDS) -> Any:
        self._start()
        self._init_event.wait(timeout=10.0)
        if self._loop is None or self._pipeline is None or not self._ready:
            raise RuntimeError("CANONICAL_RAG_NOT_READY")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout)

    # -- readiness / status ---------------------------------------------------

    def is_ready(self) -> bool:
        """Ready only while the governed state is CANONICAL.

        DEGRADED / RECONCILIATION_FAILED kick a bounded recovery attempt on
        the pipeline loop and report not-ready (restricted degraded mode);
        RECONCILING means a recovery is already in flight.
        """
        if not self._enabled:
            return False
        self._start()
        self._init_event.wait(timeout=10.0)
        pipeline, loop = self._pipeline, self._loop
        if pipeline is None or loop is None:
            return self._ready
        state = pipeline.state_machine.effective_state
        if state == "CANONICAL":
            self._ready = True
            return True
        if state in ("DEGRADED", "RECONCILIATION_FAILED"):
            try:
                asyncio.run_coroutine_threadsafe(
                    pipeline.attempt_recovery(), loop
                )
            except RuntimeError:
                pass
        self._ready = False
        return False

    def peek_ready(self) -> bool:
        """Non-blocking readiness snapshot.

        Never starts the adapter thread and never waits on the init event —
        safe for latency-sensitive call sites (e.g. inference grounding)
        that must not stall on canonical startup.
        """
        return bool(self._enabled and self._ready)

    def mark_unhealthy(self, error: str) -> None:
        self._ready = False
        self._last_error = error
        pipeline, loop = self._pipeline, self._loop
        if pipeline is not None and loop is not None:
            try:
                loop.call_soon_threadsafe(
                    pipeline.state_machine.report_canonical_failure, error
                )
            except RuntimeError:
                pass

    # Canonical takeover surface states (RAG-02): the internal A374 machine
    # keeps STARTING/CANONICAL/DEGRADED/RECONCILING; the gateway exposes the
    # governed five-state model.
    _SURFACE_STATES = {
        "STARTING": "BOOTSTRAPPING",
        "CANONICAL": "CANONICAL_READY",
        "DEGRADED": "DEGRADED_READY",
        "RECONCILING": "RECONCILING",
        "RECONCILIATION_FAILED": "DEGRADED_READY",
    }

    def _runtime_status(self) -> dict[str, Any]:
        """Pull the governed runtime state off the pipeline's state machine."""
        pipeline, loop = self._pipeline, self._loop
        if pipeline is None or loop is None:
            if self._init_event.is_set() or not self._enabled:
                state = "DEGRADED"  # init attempt finished without a pipeline
            else:
                state = "STARTING"
            return {"state": state, "effective_state": state}
        try:
            future = asyncio.run_coroutine_threadsafe(
                pipeline.health_check(), loop
            )
            return dict(future.result(timeout=5.0))
        except Exception as exc:
            return {"state": "DEGRADED", "effective_state": "DEGRADED",
                    "last_error": str(exc)}

    def _gateway_state(self, runtime: dict[str, Any]) -> str:
        """Map internal state to the governed takeover surface state."""
        if runtime.get("blocked") or runtime.get("blocked_reason"):
            return "BLOCKED"
        effective = str(runtime.get("effective_state") or "DEGRADED")
        return self._SURFACE_STATES.get(effective, "DEGRADED_READY")

    def status(self) -> dict[str, Any]:
        if self._enabled:
            self._start()
            self._init_event.wait(timeout=10.0)
        runtime = self._runtime_status()
        gateway_state = self._gateway_state(runtime)
        return {
            "engine": "canonical-vector-postgresql",
            "canonical": gateway_state == "CANONICAL_READY",
            "ready": self._ready,
            "enabled": self._enabled,
            "runtime": runtime,
            "runtime_state": gateway_state,
            "gateway_state": gateway_state,
            "pipeline_state": runtime.get("effective_state", "DEGRADED"),
            "blocked": gateway_state == "BLOCKED",
            "blocked_reason": runtime.get("blocked_reason"),
            "reconciliation_required": runtime.get(
                "reconciliation_required", True
            ),
            "collection": os.environ.get("VECTOR_COLLECTION", _DEFAULT_COLLECTION),
            "vectord_url": os.environ.get("VECTORD_URL", _DEFAULT_VECTORD_URL),
            "postgresql": "configured"
            if os.environ.get("GPTBRIDGE_POSTGRES_DSN")
            else "dsn-missing",
            "last_error": self._last_error or runtime.get("last_error"),
        }

    def record_degraded_mutation(self, document_record: dict[str, Any]) -> bool:
        """Queue a durable pending_rag_mutation for a degraded-mirror write
        so canonical recovery replays it (re-fetch → re-chunk → re-embed)."""
        pipeline, loop = self._pipeline, self._loop
        if pipeline is None or loop is None:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                pipeline._enqueue_document_mutation(document_record, []), loop
            )
            future.result(timeout=10.0)
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    def close(self) -> None:
        self._enabled = False
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)

    # -- canonical reads -----------------------------------------------------

    def query_vector(
        self,
        vector: Sequence[float],
        *,
        module_ids: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Canonical dense retrieval with index_state proof (A373/A374)."""
        return self._submit(
            self._pipeline.vector_search(
                [float(v) for v in vector],
                module_ids=module_ids,
                top_k=int(limit),
            )
        )

    def keyword_search(
        self,
        query: str,
        *,
        module_ids: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Canonical PostgreSQL FTS keyword channel."""
        return self._submit(
            self._pipeline.keyword_search(
                query, module_ids=module_ids, limit=int(limit)
            )
        )

    def query_vector_and_keyword(
        self,
        vector: Sequence[float],
        question: str,
        *,
        module_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Dense + keyword retrieval submitted concurrently on the loop.

        Both coroutines are posted before either result is awaited, so the
        vectord round trip and the PostgreSQL FTS scan overlap — wall time
        becomes max(dense, keyword) instead of the serial sum.  Results are
        identical to calling ``query_vector`` then ``keyword_search``.
        """
        self._start()
        self._init_event.wait(timeout=10.0)
        if self._loop is None or self._pipeline is None or not self._ready:
            raise RuntimeError("CANONICAL_RAG_NOT_READY")
        f_dense = asyncio.run_coroutine_threadsafe(
            self._pipeline.vector_search(
                [float(v) for v in vector],
                module_ids=module_ids,
                top_k=int(limit),
            ),
            self._loop,
        )
        f_keyword = asyncio.run_coroutine_threadsafe(
            self._pipeline.keyword_search(
                question, module_ids=module_ids, limit=int(limit)
            ),
            self._loop,
        )
        return (
            f_dense.result(timeout=_CALL_TIMEOUT_SECONDS),
            f_keyword.result(timeout=_CALL_TIMEOUT_SECONDS),
        )

    def query_text(
        self,
        question: str,
        *,
        module_ids: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Canonical dense retrieval by raw text (PERF-07): vectord embeds
        the query internally — no vector crosses the Python boundary."""
        return self._submit(
            self._pipeline.text_search(
                str(question), module_ids=module_ids, top_k=int(limit)
            )
        )

    def query_text_and_keyword(
        self,
        question: str,
        *,
        module_ids: tuple[str, ...],
        limit: int,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Text dense + keyword retrieval submitted concurrently (PERF-07):
        the query vector never materialises on the Python side."""
        self._start()
        self._init_event.wait(timeout=10.0)
        if self._loop is None or self._pipeline is None or not self._ready:
            raise RuntimeError("CANONICAL_RAG_NOT_READY")
        f_dense = asyncio.run_coroutine_threadsafe(
            self._pipeline.text_search(
                str(question), module_ids=module_ids, top_k=int(limit)
            ),
            self._loop,
        )
        f_keyword = asyncio.run_coroutine_threadsafe(
            self._pipeline.keyword_search(
                str(question), module_ids=module_ids, limit=int(limit)
            ),
            self._loop,
        )
        return (
            f_dense.result(timeout=_CALL_TIMEOUT_SECONDS),
            f_keyword.result(timeout=_CALL_TIMEOUT_SECONDS),
        )

    def embed_bytes(self, texts: Sequence[str]) -> Optional[list[Any]]:
        """Canonical f64-le embedding records from vectord ``/v1/embed``
        (binary response, ``memoryview`` slices).  ``None`` when the engine
        lacks the capability — callers keep the local embed path.

        Tries the managed pipeline first; when the pipeline is degraded but
        vectord itself still answers, a direct loopback call keeps the
        degraded mirror byte-identical to the canonical embedding."""
        if not self._enabled:
            return None
        items = [str(t) for t in texts]
        try:
            return self._submit(
                self._pipeline.vector.embed_texts(items)
            )
        except RuntimeError:
            pass
        try:
            _ensure_import_paths(_resolve_project_root(self._tool_root))
            from core_system.rag.rust_vector_runtime import VectordClient

            dimension = int(
                getattr(self._native_runtime, "EMBEDDING_DIMENSION", 1024)
            )
            return VectordClient(
                os.environ.get("VECTORD_URL", _DEFAULT_VECTORD_URL)
            ).embed_texts(items, dimension)
        except Exception:
            return None

    def fetch_document(
        self, *, module_id: str, resource_id: str
    ) -> Optional[dict[str, Any]]:
        """Canonical document lookup for ingest deduplication."""
        try:
            return self._submit(
                self._pipeline.postgresql.fetch_document(module_id, resource_id)
            )
        except RuntimeError:
            return None

    # -- canonical writes ------------------------------------------------------

    def index_document(
        self,
        *,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
    ) -> bool:
        """Canonical write-through: vectord points + PG resource/chunk/index_state."""
        dimension = len(vectors[0]) if vectors else 0
        return bool(
            self._submit(
                self._pipeline.index_document(
                    document=document,
                    chunks=chunks,
                    vectors=vectors,
                    collection_dimension=dimension,
                ),
                timeout=max(_CALL_TIMEOUT_SECONDS, 5.0 * len(chunks)),
            )
        )

    def index_document_text(
        self,
        *,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        embedding_records: list[Any],
    ) -> bool:
        """Text-mode canonical write (PERF-07): PG binds the canonical
        f64-le embedding bytes; vectord re-derives the index vectors from
        chunk content — no float list or vector JSON crosses the wire."""
        dimension = len(embedding_records[0]) // 8 if embedding_records else 0
        return bool(
            self._submit(
                self._pipeline.index_document_text(
                    document=document,
                    chunks=chunks,
                    embedding_records=embedding_records,
                    collection_dimension=dimension,
                ),
                timeout=max(_CALL_TIMEOUT_SECONDS, 5.0 * len(chunks)),
            )
        )


__all__ = ["CanonicalRagAdapter"]
