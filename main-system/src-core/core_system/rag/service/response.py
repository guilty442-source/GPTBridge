"""RagResponse — retrieval and answer stay distinct, fully
debuggable through metadata sections."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..orchestration.evidence import Citation, RagEvidence


@dataclass(frozen=True, slots=True)
class RetrievalMetadata:
    architectures: tuple[str, ...] = ()
    canonical: bool = True
    degraded_used: bool = False
    candidate_count: int = 0
    reranked_count: int = 0
    selected_count: int = 0
    rounds: int = 1
    sufficiency: str = ""


@dataclass(frozen=True, slots=True)
class GenerationMetadata:
    mode: str = "general"
    model_hint: str = ""


@dataclass(frozen=True, slots=True)
class PolicyMetadata:
    policy_version: str = ""
    admission: str = ""
    resource_grants: int = 0
    resource_denies: int = 0


@dataclass(frozen=True, slots=True)
class HealthMetadata:
    backend_state: str = ""
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class RagResponse:
    """The single response shape for query() — answer separated
    from the evidence that supports it."""

    request_id: str
    answer: str = ""
    evidence: tuple[RagEvidence, ...] = ()
    citations: tuple[Citation, ...] = ()
    retrieval: RetrievalMetadata = field(default_factory=RetrievalMetadata)
    generation: GenerationMetadata = field(default_factory=GenerationMetadata)
    policy: PolicyMetadata = field(default_factory=PolicyMetadata)
    health: HealthMetadata = field(default_factory=HealthMetadata)
    error: str = ""


__all__ = [
    "GenerationMetadata",
    "HealthMetadata",
    "PolicyMetadata",
    "RagResponse",
    "RetrievalMetadata",
]
