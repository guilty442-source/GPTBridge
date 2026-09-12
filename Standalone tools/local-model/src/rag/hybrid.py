"""Hybrid RAG — dense + sparse + semantic fusion retrieval.

Per A52, Hybrid RAG combines dense vector retrieval, sparse keyword retrieval,
and semantic fusion for general knowledge retrieval.  The shared index backend
is Qdrant (local-owned, local-only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

HYBRID_RAG_ID: Final[str] = "hybrid-rag"
RETRIEVAL_METHODS: Final[tuple[str, ...]] = ("dense", "sparse", "semantic-fusion")


@dataclass
class HybridRetrievalRequest:
    """A hybrid retrieval request (decision-level)."""
    query: str
    methods: tuple[str, ...] = RETRIEVAL_METHODS
    top_k: int = 10
    fusion_strategy: str = "weighted-rrf"  # reciprocal rank fusion


@dataclass
class HybridRetrievalResult:
    """A hybrid retrieval result."""
    query: str
    documents: list[dict] = field(default_factory=list)
    scores: list[float] = field(default_factory=list)
    method_used: tuple[str, ...] = RETRIEVAL_METHODS
    basis: str = "codex"


def retrieve(query: str, top_k: int = 10) -> HybridRetrievalResult:
    """Decision-level hybrid retrieval interface.

    Actual retrieval is delegated to the Qdrant governed executor.
    """
    return HybridRetrievalResult(query=query)


__all__ = [
    "HYBRID_RAG_ID",
    "HybridRetrievalRequest",
    "HybridRetrievalResult",
    "RETRIEVAL_METHODS",
    "retrieve",
]
