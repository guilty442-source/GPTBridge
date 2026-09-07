"""Agentic RAG — multi-step retrieve + reason + adapt.

Per A52, Agentic RAG performs multi-step retrieval, reasoning, and adaptation
for complex query reasoning.  It iteratively refines retrieval based on
intermediate results, coordinating with the Xingcheng cognition layer for
reasoning steps.  The shared index backend is Qdrant (local-owned, local-only).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

AGENTIC_RAG_ID: Final[str] = "agentic-rag"
MAX_STEPS: Final[int] = 5


@dataclass
class AgenticStep:
    """A single agentic RAG step."""
    step_number: int
    query: str
    retrieved: list[dict] = field(default_factory=list)
    reasoning: str = ""
    next_action: str = ""  # retrieve-more / reason / conclude


@dataclass
class AgenticRetrievalRequest:
    """An agentic retrieval request (decision-level)."""
    query: str
    max_steps: int = MAX_STEPS
    reasoning_basis: str = "codex"


@dataclass
class AgenticRetrievalResult:
    """An agentic retrieval result."""
    query: str
    steps: list[AgenticStep] = field(default_factory=list)
    final_answer: str = ""
    total_retrieved: int = 0
    basis: str = "codex"


def retrieve_agentic(query: str, max_steps: int = MAX_STEPS) -> AgenticRetrievalResult:
    """Decision-level agentic retrieval interface.

    Actual multi-step retrieval is delegated to the Qdrant governed executor
    and the Xingcheng cognition layer for reasoning.
    """
    return AgenticRetrievalResult(query=query)


__all__ = [
    "AGENTIC_RAG_ID",
    "AgenticRetrievalRequest",
    "AgenticRetrievalResult",
    "AgenticStep",
    "MAX_STEPS",
    "retrieve_agentic",
]
