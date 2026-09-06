"""Memory RAG — session + long-term + episodic memory retrieval.

Per A52, Memory RAG manages session, long-term, and episodic memory for
contextual memory retrieval.  It provides continuity across interactions
and historical context.  The shared index backend is Qdrant (local-owned,
local-only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

MEMORY_RAG_ID: Final[str] = "memory-rag"
MEMORY_TYPES: Final[tuple[str, ...]] = ("session", "long-term", "episodic")


@dataclass
class MemoryEntry:
    """A memory entry in the Memory RAG store."""
    memory_type: str  # one of MEMORY_TYPES
    content: str
    session_id: str = ""
    timestamp: str = ""
    metadata: dict = field(default_factory=dict)


@dataclass
class MemoryRetrievalRequest:
    """A memory retrieval request (decision-level)."""
    query: str
    memory_types: tuple[str, ...] = MEMORY_TYPES
    session_id: str = ""
    top_k: int = 10


@dataclass
class MemoryRetrievalResult:
    """A memory retrieval result."""
    query: str
    memories: list[MemoryEntry] = field(default_factory=list)
    basis: str = "codex"


def retrieve_memory(query: str, session_id: str = "", top_k: int = 10) -> MemoryRetrievalResult:
    """Decision-level memory retrieval interface.

    Actual retrieval is delegated to the Qdrant governed executor.
    """
    return MemoryRetrievalResult(query=query)


def store_memory(entry: MemoryEntry) -> bool:
    """Decision-level memory storage interface.

    Actual storage is delegated to the Qdrant governed executor.
    """
    return True


__all__ = [
    "MEMORY_RAG_ID",
    "MEMORY_TYPES",
    "MemoryEntry",
    "MemoryRetrievalRequest",
    "MemoryRetrievalResult",
    "retrieve_memory",
    "store_memory",
]
