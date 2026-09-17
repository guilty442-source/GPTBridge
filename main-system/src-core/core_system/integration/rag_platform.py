"""Composition root for the RAG backend assembly.

Instantiates the canonical backend, the bounded degraded backend and the
``RagArchitectureRouter`` from injected runtime authorities; nothing here
changes the existing classes' behaviour.  ``RagArchitectureRouter.search``
is already async and is delegated asynchronously by :class:`RagServices`.

Fail-closed wiring: a missing pipeline config, Qdrant runtime or PostgreSQL
metadata authority raises :class:`IntegrationWireError` before any backend
is constructed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ..rag.architecture_router import RagArchitectureRouter
from ..rag.canonical_backend import CanonicalRagBackend
from ..rag.degraded_backend import DegradedRagBackend
from ..rag.health_gate import CanonicalHealthGate
from ..rag.rag_qdrant import RagPipelineConfig
from .data_platform import IntegrationWireError


@dataclass
class RagServices:
    """Assembled RAG backends plus the routing facade."""

    config: RagPipelineConfig
    canonical: CanonicalRagBackend
    degraded: DegradedRagBackend
    health_gate: CanonicalHealthGate
    router: RagArchitectureRouter

    async def search(self, request: Any) -> Any:
        return await self.router.search(request)


def build_rag_router(
    config: RagPipelineConfig,
    *,
    qdrant: Any,
    postgresql: Any,
    generation_manager: Any = None,
    outbox_repo: Any = None,
    embedding_provider: Any = None,
    embedding_runtime: Any = None,
    degraded_root: Optional[str | Path] = None,
) -> RagServices:
    """Instantiate canonical/degraded backends and the architecture router.

    ``qdrant`` and ``postgresql`` are the canonical runtime authorities
    (``QdrantCanonicalRuntime`` / ``PostgreSQLMetadataAuthority`` or their
    governed stand-ins); generation manager and outbox repository are
    forwarded to the canonical backend and the health gate.
    """
    if not isinstance(config, RagPipelineConfig):
        raise IntegrationWireError("RAG_PIPELINE_CONFIG_REQUIRED")
    if qdrant is None:
        raise IntegrationWireError("RAG_QDRANT_RUNTIME_REQUIRED")
    if postgresql is None:
        raise IntegrationWireError("RAG_POSTGRESQL_AUTHORITY_REQUIRED")

    canonical = CanonicalRagBackend(
        config,
        qdrant,
        postgresql,
        generation_manager,
        outbox_repo,
        embedding_provider,
    )
    degraded = DegradedRagBackend(
        config,
        Path(degraded_root) if degraded_root is not None else None,
    )
    health_gate = CanonicalHealthGate(
        qdrant,
        postgresql,
        generation_manager,
        outbox_repo,
        embedding_runtime,
    )
    router = RagArchitectureRouter(canonical, degraded, health_gate)
    return RagServices(
        config=config,
        canonical=canonical,
        degraded=degraded,
        health_gate=health_gate,
        router=router,
    )


__all__ = [
    "RagServices",
    "build_rag_router",
]
