"""Qdrant shared index backend — local-owned semantic index.

Per A52/E38, all four RAG sub-architectures share Qdrant (local-owned,
local-only) as the semantic index backend.  This module provides the
decision-level interface to the Qdrant governed executor.

Qdrant is a formal tool declared in A49/E35.  It must be locally owned and
locally hosted; external/cloud hosting is FORBIDDEN.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

QDRANT_ID: Final[str] = "qdrant"
QDRANT_OWNERSHIP: Final[str] = "local-owned"
QDRANT_HOSTING: Final[str] = "local-only"

DEFAULT_COLLECTIONS: Final[tuple[str, ...]] = (
    "hybrid-rag",
    "code-rag",
    "agentic-rag",
    "memory-rag",
)


@dataclass
class QdrantCollectionStatus:
    """Status of a Qdrant collection."""
    name: str
    vector_size: int = 0
    points_count: int = 0
    indexed: bool = False


@dataclass
class QdrantBackendStatus:
    """Overall Qdrant backend status (decision-level)."""
    backend: str = QDRANT_ID
    ownership: str = QDRANT_OWNERSHIP
    hosting: str = QDRANT_HOSTING
    collections: list[QdrantCollectionStatus] = field(default_factory=list)
    connected: bool = False
    basis: str = "codex"


def backend_status() -> QdrantBackendStatus:
    """Return the Qdrant backend status snapshot.

    Actual connectivity check is delegated to the Qdrant governed executor.
    """
    return QdrantBackendStatus(
        collections=[
            QdrantCollectionStatus(name=name) for name in DEFAULT_COLLECTIONS
        ],
    )


__all__ = [
    "DEFAULT_COLLECTIONS",
    "QDRANT_HOSTING",
    "QDRANT_ID",
    "QDRANT_OWNERSHIP",
    "QdrantBackendStatus",
    "QdrantCollectionStatus",
    "backend_status",
]
