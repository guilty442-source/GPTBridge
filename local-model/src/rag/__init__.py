"""RAG four-sub-architecture — A52/E38 implementation.

Per the Governance Codex (A52 / E38), the RAG architecture consists of four
sub-architectures sharing Qdrant (local-owned) as the semantic index backend:

  1. Hybrid RAG    — dense + sparse + semantic fusion retrieval
  2. Code RAG      — code snippet + AST + dependency graph retrieval
  3. Agentic RAG   — multi-step retrieve + reason + adapt
  4. Memory RAG    — session + long-term + episodic memory

All four are local-owned and local-only hosted.  Non-formal RAG substitution,
external/cloud hosting, replacing the hybrid architecture, or omitting any
sub-architecture is FORBIDDEN.

This package declares the four-sub-architecture surface; actual retrieval
execution is delegated to the governed executor (Qdrant loopback).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

RAG_ARCH_VERSION: Final[str] = "rag-v1"
SHARED_INDEX_BACKEND: Final[str] = "qdrant"
OWNERSHIP: Final[str] = "local-owned"
HOSTING: Final[str] = "local-only"

SUB_ARCHITECTURES: Final[tuple[str, ...]] = (
    "hybrid-rag",
    "code-rag",
    "agentic-rag",
    "memory-rag",
)


@dataclass(frozen=True)
class RAGSubArchitecture:
    """A RAG sub-architecture declaration."""
    id: str
    method: str
    scope: str
    index_backend: str = SHARED_INDEX_BACKEND
    ownership: str = OWNERSHIP
    hosting: str = HOSTING
    basis: str = "codex"


RAG_SUB_ARCHITECTURES: Final[tuple[RAGSubArchitecture, ...]] = (
    RAGSubArchitecture(
        id="hybrid-rag",
        method="dense+sparse+semantic-fusion",
        scope="general-knowledge-retrieval",
    ),
    RAGSubArchitecture(
        id="code-rag",
        method="code-snippet+ast+dependency-graph-retrieval",
        scope="source-code-retrieval",
    ),
    RAGSubArchitecture(
        id="agentic-rag",
        method="multi-step-retrieve+reason+adapt",
        scope="complex-query-reasoning",
    ),
    RAGSubArchitecture(
        id="memory-rag",
        method="session+long-term+episodic-memory",
        scope="contextual-memory-retrieval",
    ),
)


def sub_architecture_by_id(sub_id: str) -> RAGSubArchitecture | None:
    """Look up a sub-architecture by id."""
    for sub in RAG_SUB_ARCHITECTURES:
        if sub.id == sub_id:
            return sub
    return None


def validate_architecture(arch_ids: tuple[str, ...]) -> bool:
    """Validate that all four sub-architectures are present (A52 prohibition)."""
    return set(arch_ids) == set(SUB_ARCHITECTURES)


__all__ = [
    "HOSTING",
    "OWNERSHIP",
    "RAG_ARCH_VERSION",
    "RAG_SUB_ARCHITECTURES",
    "RAGSubArchitecture",
    "SHARED_INDEX_BACKEND",
    "SUB_ARCHITECTURES",
    "sub_architecture_by_id",
    "validate_architecture",
]
