"""Vectord shared index backend - local-owned semantic index.

Per A52/E38, all four RAG sub-architectures share the Rust vectord engine
(local-owned, local-only) as the semantic index backend.  This module
provides the decision-level interface to the vectord governed executor.

A8/A611: the Rust vector engine is the canonical semantic index; Qdrant is
retired (sealed cutover).  Local ownership and local-only hosting are
required; external/cloud hosting is FORBIDDEN.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

VECTOR_ID: Final[str] = "vectord"
VECTOR_OWNERSHIP: Final[str] = "local-owned"
VECTOR_HOSTING: Final[str] = "local-only"

DEFAULT_COLLECTIONS: Final[tuple[str, ...]] = (
    "hybrid-rag",
    "code-rag",
    "agentic-rag",
    "memory-rag",
)


@dataclass
class VectordCollectionStatus:
    """Status of a vectord collection."""
    name: str
    vector_size: int = 0
    points_count: int = 0
    indexed: bool = False


@dataclass
class VectordBackendStatus:
    """Overall vectord backend status (decision-level)."""
    backend: str = VECTOR_ID
    ownership: str = VECTOR_OWNERSHIP
    hosting: str = VECTOR_HOSTING
    collections: list[VectordCollectionStatus] = field(default_factory=list)
    connected: bool = False
    basis: str = "codex"


def backend_status() -> VectordBackendStatus:
    """Return the vectord backend status snapshot.

    Actual connectivity check is delegated to the vectord governed executor.
    """
    return VectordBackendStatus(
        collections=[
            VectordCollectionStatus(name=name) for name in DEFAULT_COLLECTIONS
        ],
    )


__all__ = [
    "DEFAULT_COLLECTIONS",
    "VECTOR_HOSTING",
    "VECTOR_ID",
    "VECTOR_OWNERSHIP",
    "VectordBackendStatus",
    "VectordCollectionStatus",
    "backend_status",
]
