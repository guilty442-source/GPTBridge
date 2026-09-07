"""Code RAG — code snippet + AST + dependency graph retrieval.

Per A52, Code RAG retrieves code snippets, abstract syntax trees, and
dependency graphs for source-code retrieval.  The shared index backend is
Qdrant (local-owned, local-only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

CODE_RAG_ID: Final[str] = "code-rag"
CODE_RETRIEVAL_TYPES: Final[tuple[str, ...]] = ("snippet", "ast", "dependency-graph")


@dataclass
class CodeRetrievalRequest:
    """A code retrieval request (decision-level)."""
    query: str
    retrieval_types: tuple[str, ...] = CODE_RETRIEVAL_TYPES
    language_filter: str = ""  # python/typescript/cpp/c
    top_k: int = 10


@dataclass
class CodeRetrievalResult:
    """A code retrieval result."""
    query: str
    snippets: list[dict] = field(default_factory=list)
    ast_nodes: list[dict] = field(default_factory=list)
    dependency_edges: list[dict] = field(default_factory=list)
    basis: str = "codex"


def retrieve_code(query: str, language: str = "", top_k: int = 10) -> CodeRetrievalResult:
    """Decision-level code retrieval interface.

    Actual retrieval is delegated to the Qdrant governed executor.
    """
    return CodeRetrievalResult(query=query)


__all__ = [
    "CODE_RAG_ID",
    "CODE_RETRIEVAL_TYPES",
    "CodeRetrievalRequest",
    "CodeRetrievalResult",
    "retrieve_code",
]
