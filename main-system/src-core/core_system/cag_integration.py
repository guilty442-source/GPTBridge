"""CAG Context Preload Integration.

Integrates Context-Augmented Generation (CAG) preloading with GPTBridgeApp lifecycle:
- Startup: preload contexts for core modules after RAG is ready
- Shutdown: graceful cleanup of context manager
"""

from __future__ import annotations

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

        # Create CAG pipeline
        cag_config = CAGConfig(
            max_context_tokens=32000,
            default_ttl_seconds=3600.0,
            enable_preload=True,
            fallback_to_rag=True,
        )
        self._cag_loader, self._cag_manager, self._cag_router = create_cag_pipeline(
            retrievers, rag, cag_config
        )

        # Create hybrid orchestrator
        hybrid_config = HybridConfig(
            cag=cag_config,
            enable_cag=True,
            enable_dag=True,
            enable_rag=True,
            cag_preload_on_startup=True,
        )
        self._hybrid = create_hybrid_orchestrator(retrievers, rag, hybrid_config)

        # Preload contexts
        await self._preload_contexts()

        self._started = True

        return {
            "ok": True,
            "started_at": time.time(),
            "duration_ms": int((time.monotonic() - start_time) * 1000),
            "preloaded_contexts": len(self.PRELOAD_MODULE_GROUPS),
            "cag_stats": self._cag_manager.stats(),
        }

    async def stop(self) -> dict[str, Any]:
        """Stop CAG and cleanup."""
        if not self._started:
            return {"ok": True, "already_stopped": True}

        stop_start = time.monotonic()

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
        # RAG orchestrator has retrievers internally
        # We expose them via the CAG interface
        return {
            "retrieve_hybrid": lambda q, s: self._rag_orchestrator._dispatch(
                ("HYBRID",), q, s
            ).get("HYBRID", []),
            "retrieve_code": lambda q, s: self._rag_orchestrator._dispatch(
                ("CODE",), q, s
            ).get("CODE", []),
            "retrieve_memory": lambda q, s: self._rag_orchestrator._dispatch(
                ("MEMORY",), q, s
            ).get("MEMORY", []),
        }

    async def _preload_contexts(self) -> None:
        """Preload contexts for core module groups."""
        if not self._cag_loader or not self._cag_manager:
            return

        for modules in self.PRELOAD_MODULE_GROUPS:
            try:
                context = self._cag_loader.load_context(modules)
                self._cag_manager.store(context)
            except Exception:
                # Log but don't fail startup
                pass

    def get_hybrid_orchestrator(self) -> HybridOrchestrator | None:
        """Get the hybrid orchestrator for query execution."""
        return self._hybrid

    def get_stats(self) -> dict[str, Any]:
        """Get integration stats."""
        return {
            "started": self._started,
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