"""CAG Context Preload Integration.

Integrates Context-Augmented Generation (CAG) preloading with GPTBridgeApp lifecycle:
- Startup: preload contexts for core modules after RAG is ready
- Shutdown: graceful cleanup of context manager
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from dataclasses import dataclass
from typing import Any

from shared_layer.database.config import DatabaseSettings
from shared_layer.database.connection import get_connection_manager
from shared_layer.database.sqlite_classification import list_by_class
from .rag.orchestration.orchestrator import RagOrchestrator
from .cag import (
    CAGConfig,
    ContextLoader,
    ContextManager,
    ContextRouter,
    create_cag_pipeline,
)
from .hybrid import (
    HybridConfig,
    HybridOrchestrator,
    create_hybrid_orchestrator,
)


@dataclass
class CAGIntegration:
    """CAG integration with app lifecycle."""

    app: Any
    _cag_loader: ContextLoader | None = None
    _cag_manager: ContextManager | None = None
    _cag_router: ContextRouter | None = None
    _hybrid: HybridOrchestrator | None = None
    _rag_orchestrator: RagOrchestrator | None = None
    _preload_task: "asyncio.Task[None] | None" = None
    _preload_done: bool = False
    _started: bool = False

    # Module groups to preload on startup
    PRELOAD_MODULE_GROUPS = [
        ("main-system",),
        ("shared-layer",),
        ("governance_rule",),
        ("xingcheng",),
    ]

    async def start(self) -> dict[str, Any]:
        """Start CAG preloading."""
        if self._started:
            return {"ok": True, "already_started": True}

        start_time = time.monotonic()

        # Prerequisites: RAG orchestrator must be available
        rag = getattr(self.app, "rag_orchestrator", None)
        if rag is None:
            return {"ok": False, "reason": "RAG orchestrator not initialized"}

        self._rag_orchestrator = rag

        # Build retrievers map from RAG
        retrievers = self._build_retrievers()

        # Create hybrid orchestrator — it owns the single CAG pipeline
        # (loader/manager/router).  Preloading must target that manager,
        # not a second pipeline queries never see.
        cag_config = CAGConfig(
            max_context_tokens=32000,
            default_ttl_seconds=3600.0,
            enable_preload=True,
            fallback_to_rag=True,
        )
        hybrid_config = HybridConfig(
            cag=cag_config,
            enable_cag=True,
            enable_dag=True,
            enable_rag=True,
            cag_preload_on_startup=True,
        )
        self._hybrid = create_hybrid_orchestrator(retrievers, rag, hybrid_config)
        self._cag_loader = self._hybrid._cag_loader
        self._cag_manager = self._hybrid._cag_manager
        self._cag_router = self._hybrid._cag_router

        # Context preloading runs in the background — each load performs
        # real retrieval (blocking sync calls on the RAG worker loop), so
        # running it inline would stall the main event loop and the
        # startup SLA.  Queries during warmup fall back to RAG/DAG, which
        # the hybrid selector reports truthfully.
        self._preload_task = asyncio.get_running_loop().create_task(
            self._preload_background()
        )

        self._started = True

        return {
            "ok": True,
            "started_at": time.time(),
            "duration_ms": int((time.monotonic() - start_time) * 1000),
            "preloading": True,
            "contexts_planned": len(self.PRELOAD_MODULE_GROUPS),
            "cag_stats": self._cag_manager.stats(),
        }

    async def stop(self) -> dict[str, Any]:
        """Stop CAG and cleanup."""
        if not self._started:
            return {"ok": True, "already_stopped": True}

        stop_start = time.monotonic()

        task = self._preload_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._preload_task = None

        # Clear contexts
        if self._cag_manager:
            # Context manager has no explicit stop, just clear references
            self._cag_manager = None
        self._cag_loader = None
        self._cag_router = None
        self._hybrid = None

        self._started = False

        return {
            "ok": True,
            "stopped_at": time.time(),
            "duration_ms": int((time.monotonic() - stop_start) * 1000),
        }

    def _build_retrievers(self) -> dict[str, Any]:
        """Build retriever function map from RAG orchestrator."""
        # RAG orchestrator has retrievers internally; _dispatch keys on
        # RagArchitecture enum members, not plain strings.
        from .rag.orchestration.evidence import RagArchitecture

        return {
            "retrieve_hybrid": lambda q, s: self._rag_orchestrator._dispatch(
                (RagArchitecture.HYBRID,), q, s
            ).get(RagArchitecture.HYBRID, []),
            "retrieve_code": lambda q, s: self._rag_orchestrator._dispatch(
                (RagArchitecture.CODE,), q, s
            ).get(RagArchitecture.CODE, []),
            "retrieve_memory": lambda q, s: self._rag_orchestrator._dispatch(
                (RagArchitecture.MEMORY,), q, s
            ).get(RagArchitecture.MEMORY, []),
        }

    async def _preload_background(self) -> None:
        """Wait for pipeline readiness, then preload off the event loop."""
        try:
            rag_runtime = getattr(self.app, "rag_runtime", None)
            if rag_runtime is not None:
                await rag_runtime.wait_initialized_async(timeout=60)
            loaded = await asyncio.to_thread(self._preload_contexts_sync)
            self._preload_done = True
            self.app._log(
                {
                    "type": "status",
                    "message": "CAG contexts preloaded",
                    "ok": True,
                    "preloaded_contexts": loaded,
                    "cag_stats": (
                        self._cag_manager.stats()
                        if self._cag_manager
                        else {}
                    ),
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — bounded channel
            try:
                self.app._log(
                    {
                        "type": "status",
                        "message": "CAG preload failed",
                        "ok": False,
                        "reason": str(exc),
                    }
                )
            except Exception:
                pass

    def _preload_contexts_sync(self) -> int:
        """Load contexts for core module groups (blocking — run off-loop)."""
        if not self._cag_loader or not self._cag_manager:
            return 0

        def _load(modules: tuple[str, ...]) -> Any:
            try:
                # Module names act as retrieval hints so the stored
                # context carries real documents, not an empty shell.
                return self._cag_loader.load_context(
                    modules, query_hints=tuple(modules)
                )
            except Exception:
                return None

        from shared_layer.performance.thread_budget import (
            bounded_workers,
        )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=bounded_workers(len(self.PRELOAD_MODULE_GROUPS)),
            thread_name_prefix="cag-preload",
        ) as pool:
            contexts = list(pool.map(_load, self.PRELOAD_MODULE_GROUPS))

        loaded = 0
        for context in contexts:
            if context is None:
                continue
            try:
                self._cag_manager.store(context)
                loaded += 1
            except Exception:
                pass
        return loaded

    def get_hybrid_orchestrator(self) -> HybridOrchestrator | None:
        """Get the hybrid orchestrator for query execution."""
        return self._hybrid

    def get_stats(self) -> dict[str, Any]:
        """Get integration stats."""
        return {
            "started": self._started,
            "preloading": (
                self._preload_task is not None
                and not self._preload_task.done()
            ),
            "preload_done": self._preload_done,
            "cag_stats": self._cag_manager.stats() if self._cag_manager else {},
            "hybrid_stats": self._hybrid.get_stats() if self._hybrid else {},
        }


def create_cag_integration(app: Any) -> CAGIntegration:
    """Create CAG integration for the app."""
    return CAGIntegration(app)


__all__ = [
    "CAGIntegration",
    "create_cag_integration",
]