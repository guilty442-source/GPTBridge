"""RAG retrievers — four sub-architectures on the canonical pipeline (A52).

Each retriever shares:
  * Qdrant (dense vector index)
  * PostgreSQL (metadata / FTS / index_state authority)
  * Embedding runtime (governed local model contract)
  * Reranker (Qwen3 CrossEncoder, injected)
  * Governance (governed module scope, tombstone guard)

But each has a different retrieval behavior:

  HybridRetriever    — Qdrant Dense + PG FTS + RRF
  CodeRetriever      — chunk + AST + symbol + dependency graph
  AgenticRetriever   — retrieve → evaluate → reformulate → retrieve
  MemoryRetriever    — session + episodic + long-term memory

All retrievers are transport-agnostic: they call the host pipeline's
``hybrid_search`` / ``vector_search`` / ``keyword_search`` surface and
do not own model loads (A49).
"""
from __future__ import annotations

from typing import Any, Callable, Optional, Protocol

from .hybrid import HybridRetriever
from .code import CodeRetriever
from .agentic import AgenticRetriever
from .memory import MemoryRetriever

__all__ = [
    "HybridRetriever",
    "CodeRetriever",
    "AgenticRetriever",
    "MemoryRetriever",
]
