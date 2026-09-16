"""Evidence Sufficiency — the formal gate for the agentic loop.

The agentic controller never asks an LLM "is this enough?" — it
evaluates structured criteria:

    coverage          — fraction of required aspects with evidence
    source_diversity  — distinct architectures that contributed
    authority         — best authority class present
    freshness         — worst freshness among top evidence
    contradiction     — conflicting evidence flagged by fusion

Decisions:
    SUFFICIENT           -> proceed to generation
    RETRIEVE             -> another retrieval round (coverage/diversity low)
    FIND_AUTHORITY       -> contradiction: seek canonical source
    STOP_INSUFFICIENT    -> budget exhausted; generate with caveat or
                            fail-closed per policy
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .evidence import (
    AUTHORITY_RANK,
    RagArchitecture,
    RagEvidence,
    SourceAuthority,
    authority_of,
)


class SufficiencyVerdict(str, Enum):
    SUFFICIENT = "SUFFICIENT"
    RETRIEVE = "RETRIEVE"
    FIND_AUTHORITY = "FIND_AUTHORITY"
    STOP_INSUFFICIENT = "STOP_INSUFFICIENT"


@dataclass(frozen=True, slots=True)
class SufficiencyPolicy:
    min_evidence: int = 3
    min_coverage: float = 0.5          # required aspects covered
    min_diversity: int = 1             # distinct archs required
    min_authority: SourceAuthority = SourceAuthority.DERIVED
    min_freshness: float = 0.3
    max_rounds: int = 3


@dataclass(frozen=True, slots=True)
class SufficiencyReport:
    verdict: SufficiencyVerdict
    coverage: float
    source_diversity: int
    best_authority: SourceAuthority
    min_freshness: float
    contradiction: bool
    evidence_count: int
    reasons: tuple[str, ...] = ()


def _coverage(evidence: list[RagEvidence], required_aspects: tuple[str, ...]) -> float:
    if not required_aspects:
        return 1.0 if evidence else 0.0
    covered = 0
    for aspect in required_aspects:
        if any(aspect in e.provenance.get("aspects", ()) for e in evidence):
            covered += 1
    return covered / len(required_aspects)


def evaluate_sufficiency(
    evidence: list[RagEvidence],
    *,
    policy: SufficiencyPolicy = SufficiencyPolicy(),
    required_aspects: tuple[str, ...] = (),
    round_number: int = 1,
) -> SufficiencyReport:
    """Structured sufficiency verdict for the agentic gate."""
    authorized = [e for e in evidence if e.authorized]
    diversity = len({e.rag_type for e in authorized})
    contradiction = any(e.evidence_conflict for e in authorized)
    best_auth = max(
        (authority_of(e) for e in authorized),
        key=lambda a: AUTHORITY_RANK[a],
        default=SourceAuthority.CONTEXTUAL_MEMORY,
    )
    freshness = min((e.freshness for e in authorized), default=0.0)
    coverage = _coverage(authorized, required_aspects)
    reasons: list[str] = []

    if len(authorized) < policy.min_evidence:
        reasons.append(f"evidence<{policy.min_evidence}")
    if coverage < policy.min_coverage:
        reasons.append(f"coverage<{policy.min_coverage}")
    if diversity < policy.min_diversity:
        reasons.append(f"diversity<{policy.min_diversity}")
    if freshness < policy.min_freshness and authorized:
        reasons.append("stale-evidence")

    budget_left = round_number < policy.max_rounds
    needs_authority = contradiction and (
        AUTHORITY_RANK[best_auth] < AUTHORITY_RANK[SourceAuthority.CANONICAL_SOURCE]
    )

    if needs_authority:
        reasons.append("contradiction-needs-canonical")
        verdict = (
            SufficiencyVerdict.FIND_AUTHORITY
            if budget_left
            else SufficiencyVerdict.STOP_INSUFFICIENT
        )
    elif not reasons:
        verdict = SufficiencyVerdict.SUFFICIENT
    elif budget_left:
        verdict = SufficiencyVerdict.RETRIEVE
    else:
        verdict = SufficiencyVerdict.STOP_INSUFFICIENT

    return SufficiencyReport(
        verdict=verdict,
        coverage=coverage,
        source_diversity=diversity,
        best_authority=best_auth,
        min_freshness=freshness,
        contradiction=contradiction,
        evidence_count=len(authorized),
        reasons=tuple(reasons),
    )


__all__ = [
    "SufficiencyPolicy",
    "SufficiencyReport",
    "SufficiencyVerdict",
    "evaluate_sufficiency",
]
