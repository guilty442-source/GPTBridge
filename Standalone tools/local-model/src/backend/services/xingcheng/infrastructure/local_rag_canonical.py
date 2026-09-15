"""Canonical RAG adapter — sync facade binding LocalRagService to the
CanonicalRagPipeline (A371-A374).

The canonical pipeline is async (psycopg AsyncConnection + Qdrant).  This
adapter owns a dedicated background event-loop thread (Windows selector
policy, required by psycopg) and exposes bounded synchronous methods so the
sync ``LocalRagService`` can use the live canonical path:

    query:   query_vector + keyword_search  (Qdrant dense + PG FTS/index_state)
    ingest:  index_document                 (Qdrant points + PG authority rows)

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

_DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
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
        transformer_runtime: Any = None,
        *,
        enabled: bool = True,
    ) -> None:
        self._tool_root = Path(tool_root).resolve()
        self._transformer_runtime = transformer_runtime
        self._enabled = enabled
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
            embedding_model = "qwen3-embedding:4b"
            runtime = self._transformer_runtime
            if runtime is not None:
                embedding_model = str(getattr(runtime, "EMBEDDING_MODEL", embedding_model))
            self._pipeline = CanonicalRagPipeline(
                RagPipelineConfig(
                    qdrant_url=os.environ.get("QDRANT_URL", _DEFAULT_QDRANT_URL),
                    qdrant_api_key=os.environ.get("QDRANT_API_KEY") or None,
                    collection_name=os.environ.get(
                        "QDRANT_COLLECTION", _DEFAULT_COLLECTION
                    ),
                    postgresql_dsn=dsn,
                    # Governed local contract: qwen3-embedding:4b via the
                    # local Ollama runtime, 2560-dim — matches the canonical
                    # gptbridge_shared_knowledge collection.
                    embedding_model=embedding_model,
                    embedding_dimension=2560,
                    embedding_provider="ollama",
                    chunk_size=1200,
                    chunk_overlap=200,
                    top_k=48,
                    score_threshold=0.0,
                )
            )
            self._ready = bool(await self._pipeline.initialize())
            if not self._ready:
                self._last_error = "canonical-initialize-failed"
        except Exception as exc:
            self._last_error = str(exc)
            self._pipeline = None
            self._ready = False

    def _submit(self, coroutine: Any, timeout: float = _CALL_TIMEOUT_SECONDS) -> Any:
        self._start()
        self._init_event.wait(timeout=10.0)
        if self._loop is None or self._pipeline is None or not self._ready:
            raise RuntimeError("CANONICAL_RAG_NOT_READY")
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        return future.result(timeout=timeout)

    # -- readiness / status ---------------------------------------------------

    def is_ready(self) -> bool:
        if not self._enabled:
            return False
        self._start()
        self._init_event.wait(timeout=10.0)
        return self._ready

    def mark_unhealthy(self, error: str) -> None:
        self._ready = False
        self._last_error = error

    def status(self) -> dict[str, Any]:
        if self._enabled:
            self._start()
            self._init_event.wait(timeout=10.0)
        return {
            "engine": "canonical-qdrant-postgresql",
            "canonical": True,
            "ready": self._ready,
            "enabled": self._enabled,
            "collection": os.environ.get("QDRANT_COLLECTION", _DEFAULT_COLLECTION),
            "qdrant_url": os.environ.get("QDRANT_URL", _DEFAULT_QDRANT_URL),
            "postgresql": "configured"
            if os.environ.get("GPTBRIDGE_POSTGRES_DSN")
            else "dsn-missing",
            "last_error": self._last_error,
        }

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
        """Canonical write-through: Qdrant points + PG resource/chunk/index_state."""
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


__all__ = ["CanonicalRagAdapter"]
