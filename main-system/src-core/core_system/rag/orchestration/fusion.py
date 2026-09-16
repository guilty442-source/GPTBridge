"""Two-level Evidence Fusion.

Level 1 — Channel Fusion (inside one architecture):
    hybrid : dense + sparse  -> RRF
    code   : semantic + symbol + graph -> code fusion
    memory : semantic + recency + importance + confidence
             -> memory_score

Level 2 — Architecture Fusion (across hybrid/code/memory):
    combines per-arch evidence into one pool honouring authority —
    a high-similarity memory must not outrank canonical source.

Scores are never forced into one formula: each channel keeps its
own field on ``RagEvidence`` and each fusion stage computes its own
composite.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .evidence import (
    AUTHORITY_RANK,
    EvidenceKind,
    RagArchitecture,
    RagEvidence,
    SourceAuthority,
    authority_of,
)

_RRF_K = 60.0


def _by_id(evidence: Iterable[RagEvidence]) -> dict[str, RagEvidence]:
    return {e.evidence_id: e for e in evidence}


def channel_fusion_hybrid(
    dense: list[RagEvidence], sparse: list[RagEvidence]
) -> list[RagEvidence]:
    """dense + FTS -> RRF composite stored in ``dense_score``."""
    merged = _by_id(dense)
    for e in sparse:
        if e.evidence_id not in merged:
            merged[e.evidence_id] = e
    rrf: dict[str, float] = {}
    for rank, e in enumerate(dense):
        rrf[e.evidence_id] = rrf.get(e.evidence_id, 0.0) + 1.0 / (_RRF_K + rank)
    for rank, e in enumerate(sparse):
        rrf[e.evidence_id] = rrf.get(e.evidence_id, 0.0) + 1.0 / (_RRF_K + rank)
    out: list[RagEvidence] = []
    for eid, e in merged.items():
        out.append(_replace(e, dense_score=rrf.get(eid, 0.0)))
    out.sort(key=lambda e: -e.dense_score)
    return out


def channel_fusion_code(
    semantic: list[RagEvidence],
    symbol: list[RagEvidence],
    graph: list[RagEvidence],
) -> list[RagEvidence]:
    """semantic + symbol + graph -> code composite in ``graph_score``.

    Graph relations are real connectivity — they get a full rank-based
    share, not a tiny boost.
    """
    merged = _by_id(semantic)
    merged.update(_by_id(symbol))
    merged.update(_by_id(graph))
    rrf: dict[str, float] = {}
    for channel in (semantic, symbol, graph):
        for rank, e in enumerate(channel):
            rrf[e.evidence_id] = rrf.get(e.evidence_id, 0.0) + 1.0 / (_RRF_K + rank)
    out = [_replace(e, graph_score=rrf.get(eid, 0.0)) for eid, e in merged.items()]
    out.sort(key=lambda e: -e.graph_score)
    return out


def memory_score(
    semantic: float,
    recency: float,
    importance: float,
    confidence: float,
    *,
    w_semantic: float = 0.40,
    w_recency: float = 0.20,
    w_importance: float = 0.20,
    w_confidence: float = 0.20,
) -> float:
    """``semantic + recency + importance + confidence`` composite —
    memory's own score before it enters fusion."""
    return (
        w_semantic * semantic
        + w_recency * recency
        + w_importance * importance
        + w_confidence * confidence
    )


def channel_fusion_memory(
    semantic: list[RagEvidence],
    *,
    recency_of: Any = None,
    importance_of: Any = None,
    confidence_of: Any = None,
) -> list[RagEvidence]:
    """Score memory candidates with the four-factor memory_score."""
    out: list[RagEvidence] = []
    for e in semantic:
        score = memory_score(
            e.dense_score,
            recency_of(e) if recency_of else e.freshness,
            importance_of(e) if importance_of else float(e.provenance.get("importance", 0.5)),
            confidence_of(e) if confidence_of else float(e.provenance.get("confidence", 0.5)),
        )
        out.append(_replace(e, memory_score=score))
    out.sort(key=lambda e: -e.memory_score)
    return out


def _replace(e: RagEvidence, **kw: Any) -> RagEvidence:
    import dataclasses
    return dataclasses.replace(e, **kw)


def _channel_score(e: RagEvidence) -> float:
    """Best available per-arch composite score."""
    if e.rag_type is RagArchitecture.MEMORY:
        return e.memory_score or e.dense_score
    if e.rag_type is RagArchitecture.CODE:
        return e.graph_score or e.dense_score
    return e.dense_score or e.reranker_score


def architecture_fusion(
    pools: dict[RagArchitecture, list[RagEvidence]],
    *,
    authority_bonus: float = 0.05,
) -> list[RagEvidence]:
    """Merge per-architecture pools into one ordered list.

    Composite = channel score + authority_bonus * rank.  Canonical
    source gets a structural advantage over memory even when a memory
    chunk happens to be more similar — but memory is never excluded,
    only correctly weighted.
    """
    all_ev: list[RagEvidence] = []
    for arch, pool in pools.items():
        all_ev.extend(pool)
    scored = [
        (
            _channel_score(e)
            + authority_bonus * AUTHORITY_RANK[authority_of(e)],
            e,
        )
        for e in all_ev
    ]
    scored.sort(key=lambda t: -t[0])
    return [e for _, e in scored]


def mark_conflicts(evidence: list[RagEvidence]) -> list[RagEvidence]:
    """Flag contradicting evidence instead of dropping it.

    Two evidence units conflict when they carry the same fact-slot
    (``provenance["fact_key"]``) with different ``fact_value``.  All
    conflicting units are kept and flagged so the answer can explain
    the discrepancy — authority ordering happens at fusion, not by
    deletion.
    """
    slots: dict[str, set[str]] = {}
    for e in evidence:
        fk = e.provenance.get("fact_key")
        fv = e.provenance.get("fact_value")
        if fk is not None and fv is not None:
            slots.setdefault(str(fk), set()).add(str(fv))
    conflict_keys = {k for k, v in slots.items() if len(v) > 1}
    if not conflict_keys:
        return evidence
    return [
        _replace(e, evidence_conflict=True)
        if str(e.provenance.get("fact_key", "")) in conflict_keys
        else e
        for e in evidence
    ]


__all__ = [
    "architecture_fusion",
    "channel_fusion_code",
    "channel_fusion_hybrid",
    "channel_fusion_memory",
    "mark_conflicts",
    "memory_score",
]
