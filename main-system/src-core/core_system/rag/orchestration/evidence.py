"""Unified Evidence Contract (four-RAG formal relationship).

Every retriever — hybrid, code, memory, and every agentic round —
emits ``RagEvidence``.  Fusion, rerank, sufficiency, context builder
and citation all consume this single type; no component ever sees
four different result shapes.

Authority is explicit and never blended into similarity scores:

    canonical-source  >  derived  >  contextual-memory

A highly-similar old memory must not outrank canonical knowledge;
authority lives in a separate field so fusion can honour it.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class RagArchitecture(str, Enum):
    """Formal sub-architecture identifiers (stable codes)."""

    HYBRID = "hybrid-rag"
    CODE = "code-rag"
    MEMORY = "memory-rag"
    AGENTIC = "agentic-rag"


DEFAULT_ARCHITECTURE = RagArchitecture.HYBRID


class EvidenceKind(str, Enum):
    """Formal evidence kinds — Context Builder treats each
    differently (whole function, adjacency merge, scope display,
    structured relation)."""

    SOURCE_TEXT = "SOURCE_TEXT"
    CODE_SNIPPET = "CODE_SNIPPET"
    SYMBOL = "SYMBOL"
    DEPENDENCY = "DEPENDENCY"
    MEMORY = "MEMORY"
    DERIVED = "DERIVED"
    STRUCTURED_DATA = "STRUCTURED_DATA"


class SourceAuthority(str, Enum):
    """Authority class — ordering beats similarity on conflict.

    Six formal levels; a high-similarity memory or degraded cache hit
    can never outrank canonical source just because cosine is bigger.
    """

    CANONICAL_SOURCE = "canonical-source"
    STRUCTURED_AUTHORITY = "structured-authority"
    CODE_CURRENT = "code-current"
    DERIVED = "derived"
    CONTEXTUAL_MEMORY = "contextual-memory"
    DEGRADED_CACHE = "degraded-cache"


AUTHORITY_RANK: dict[SourceAuthority, int] = {
    SourceAuthority.CANONICAL_SOURCE: 6,
    SourceAuthority.STRUCTURED_AUTHORITY: 5,
    SourceAuthority.CODE_CURRENT: 4,
    SourceAuthority.DERIVED: 3,
    SourceAuthority.CONTEXTUAL_MEMORY: 2,
    SourceAuthority.DEGRADED_CACHE: 1,
}


class MemoryKind(str, Enum):
    """Memory taxonomy — an episodic note is not a long-term fact."""

    SESSION = "session"
    EPISODIC = "episodic"
    LONG_TERM = "long_term"
    PREFERENCE = "preference"
    DERIVED = "derived"


@dataclass(frozen=True, slots=True)
class RagEvidence:
    """One evidence unit from any retriever or agentic round."""

    evidence_id: str
    rag_type: RagArchitecture

    # Identity
    module_id: str = ""
    resource_id: str = ""
    chunk_id: str = ""
    locator_id: str = ""

    evidence_kind: EvidenceKind = EvidenceKind.SOURCE_TEXT

    # Provenance / versioning
    generation_id: str = ""
    source_version: int = 0
    content_hash: str = ""

    # Channel scores — kept separate; fusion decides how they combine
    dense_score: float = 0.0
    sparse_score: float = 0.0
    graph_score: float = 0.0
    memory_score: float = 0.0
    reranker_score: float = 0.0

    # Authority / admission
    canonical: bool = True
    authorized: bool = True
    freshness: float = 1.0        # 0..1, 1 = current generation
    authority: SourceAuthority = SourceAuthority.CANONICAL_SOURCE

    # Content + lineage
    content: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    # Conflict marker — set by fusion when two evidence units
    # disagree; never silently dropped (kept for the answer to
    # explain).
    evidence_conflict: bool = False


@dataclass(frozen=True, slots=True)
class Citation:
    """Citation object that knows its evidence type."""

    label: str                    # [R1] [R2] [M1] ...
    evidence_id: str
    evidence_kind: EvidenceKind
    rag_type: RagArchitecture
    authority: SourceAuthority
    locator_id: str = ""
    resource_id: str = ""


_CITATION_PREFIX = {
    RagArchitecture.MEMORY: "M",
    RagArchitecture.HYBRID: "R",
    RagArchitecture.CODE: "R",
    RagArchitecture.AGENTIC: "R",
}


def citation_label(index: int, evidence: RagEvidence) -> str:
    """``[R1]``/``[M1]`` style label — memory gets its own prefix so
    an answer's reliance on memory vs canonical source stays visible."""
    prefix = _CITATION_PREFIX.get(evidence.rag_type, "R")
    return f"[{prefix}{index}]"


def make_citation(index: int, evidence: RagEvidence) -> Citation:
    return Citation(
        label=citation_label(index, evidence),
        evidence_id=evidence.evidence_id,
        evidence_kind=evidence.evidence_kind,
        rag_type=evidence.rag_type,
        authority=evidence.authority,
        locator_id=evidence.locator_id,
        resource_id=evidence.resource_id,
    )


def authority_of(evidence: RagEvidence) -> SourceAuthority:
    """Derive authority from kind/rag_type when not set explicitly."""
    if evidence.authority is not SourceAuthority.CANONICAL_SOURCE:
        return evidence.authority
    if evidence.rag_type is RagArchitecture.MEMORY or (
        evidence.evidence_kind is EvidenceKind.MEMORY
    ):
        return SourceAuthority.CONTEXTUAL_MEMORY
    if evidence.evidence_kind is EvidenceKind.DERIVED:
        return SourceAuthority.DERIVED
    return SourceAuthority.CANONICAL_SOURCE


__all__ = [
    "AUTHORITY_RANK",
    "Citation",
    "DEFAULT_ARCHITECTURE",
    "EvidenceKind",
    "MemoryKind",
    "RagArchitecture",
    "RagEvidence",
    "SourceAuthority",
    "authority_of",
    "citation_label",
    "make_citation",
]
