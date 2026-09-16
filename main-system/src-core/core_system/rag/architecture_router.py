"""RAG Architecture Router — 決定使用 Canonical 或 Degraded Backend。

基於健康閘狀態路由請求。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .rag_protocol import RagIndexBackend, RagBackendHealth, BackendType
from .rag_contracts import (
    RagIndexRequest,
    RagIndexResult,
    RagDeleteRequest,
    RagDeleteResult,
    RagSearchRequest,
    RagSearchResult,
    RagReconcileRequest,
    RagReconcileResult,
    CanonicalState,
)
from .canonical_backend import CanonicalRagBackend
from .degraded_backend import DegradedRagBackend
from .health_gate import CanonicalHealthGate

_logger = logging.getLogger("gptbridge.rag.router")


class RagArchitectureRouter:
    """Routes index/search/delete requests to appropriate backend."""

    def __init__(
        self,
        canonical: CanonicalRagBackend,
        degraded: DegradedRagBackend,
        health_gate: CanonicalHealthGate,
    ) -> None:
        self.canonical = canonical
        self.degraded = degraded
        self.health_gate = health_gate
        self._current_backend: Optional[RagIndexBackend] = None

    async def get_backend(self) -> RagIndexBackend:
        """Determine and return the appropriate backend based on health."""
        health = await self.health_gate.evaluate()

        if health.state in (
            CanonicalState.CANONICAL_READY,
            CanonicalState.DEGRADED,
        ):
            # Canonical available (even if degraded, prefer canonical)
            self._current_backend = self.canonical
            _logger.debug("RagArchitectureRouter: using CanonicalRagBackend (state=%s)", health.state.value)
        else:
            # Canonical unavailable, fallback to degraded
            self._current_backend = self.degraded
            _logger.warning(
                "RagArchitectureRouter: falling back to DegradedRagBackend (state=%s)",
                health.state.value,
            )

        return self._current_backend

    def upsert_resource(self, request: Any) -> Any:
        """Route upsert to appropriate backend."""
        if self._current_backend is None:
            # Fallback decision without health check (sync path)
            health = self.canonical.health()
            if health.healthy:
                return self.canonical.upsert_resource(request)
            return self.degraded.upsert_resource(request)
        return self._current_backend.upsert_resource(request)

    def delete_resource(self, request: Any) -> Any:
        """Route delete to appropriate backend."""
        if self._current_backend is None:
            health = self.canonical.health()
            if health.healthy:
                return self.canonical.delete_resource(request)
            return self.degraded.delete_resource(request)
        return self._current_backend.delete_resource(request)

    def search(self, request: Any) -> Any:
        """Route search to appropriate backend."""
        if self._current_backend is None:
            health = self.canonical.health()
            if health.healthy:
                return self.canonical.search(request)
            return self.degraded.search(request)
        return self._current_backend.search(request)

    def reconcile(self, request: Any) -> Any:
        """Route reconciliation to appropriate backend."""
        if self._current_backend is None:
            health = self.canonical.health()
            if health.healthy:
                return self.canonical.reconcile(request)
            return self.degraded.reconcile(request)
        return self._current_backend.reconcile(request)


__all__ = ["RagArchitectureRouter"]