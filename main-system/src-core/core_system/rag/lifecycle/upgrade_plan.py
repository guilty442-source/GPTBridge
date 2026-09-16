"""Formal five-phase RAG upgrade plan — sequential, never all at
once:

    PHASE 1  Canonical takeover
    PHASE 2  Hybrid canonicalization (Qdrant + PG FTS + RRF)
    PHASE 3  Recovery (outbox + degraded + reconciliation)
    PHASE 4  Specialized RAG (code + memory + agentic)
    PHASE 5  Lifecycle (generation + migration + benchmark + DR)

A phase unlocks only when the previous phase is complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class UpgradePhase(str, Enum):
    CANONICAL_TAKEOVER = "phase-1-canonical-takeover"
    HYBRID_CANONICALIZATION = "phase-2-hybrid-canonicalization"
    RECOVERY = "phase-3-recovery"
    SPECIALIZED_RAG = "phase-4-specialized-rag"
    LIFECYCLE = "phase-5-lifecycle"


PHASE_ORDER: tuple[UpgradePhase, ...] = tuple(UpgradePhase)

PHASE_SCOPE: dict[UpgradePhase, tuple[str, ...]] = {
    UpgradePhase.CANONICAL_TAKEOVER: ("canonical-gateway", "takeover-criteria"),
    UpgradePhase.HYBRID_CANONICALIZATION: ("qdrant-dense", "pg-fts", "rrf"),
    UpgradePhase.RECOVERY: ("outbox", "degraded-backend", "reconciliation"),
    UpgradePhase.SPECIALIZED_RAG: ("code-rag", "memory-rag", "agentic-rag"),
    UpgradePhase.LIFECYCLE: ("generations", "migrations", "benchmark", "dr"),
}


def phase_allowed(
    target: UpgradePhase, completed: frozenset[UpgradePhase]
) -> tuple[bool, str]:
    """Sequential gating — phase N requires phases 1..N-1 done."""
    idx = PHASE_ORDER.index(target)
    for earlier in PHASE_ORDER[:idx]:
        if earlier not in completed:
            return False, f"requires-{earlier.value}"
    return True, "allowed"


@dataclass(frozen=True, slots=True)
class ShadowQueryReport:
    """Background comparison while a shadow generation runs —
    never affects the live answer."""

    queries_compared: int
    topk_overlap: float
    expected_hit_rate: float
    latency_delta_ms: float
    reranker_lift: float = 0.0


def evaluate_shadow(
    report: ShadowQueryReport,
    *,
    min_overlap: float = 0.6,
    min_expected_hit: float = 0.8,
    max_latency_delta_ms: float = 100.0,
) -> tuple[bool, tuple[str, ...]]:
    """Shadow validation before alias swap — overlap/expected-hit
    must hold and latency must not regress."""
    failures: list[str] = []
    if report.topk_overlap < min_overlap:
        failures.append(f"overlap {report.topk_overlap}<{min_overlap}")
    if report.expected_hit_rate < min_expected_hit:
        failures.append(f"expected-hit {report.expected_hit_rate}<{min_expected_hit}")
    if report.latency_delta_ms > max_latency_delta_ms:
        failures.append(f"latency-delta {report.latency_delta_ms}>{max_latency_delta_ms}")
    return (not failures, tuple(failures))


__all__ = [
    "PHASE_ORDER",
    "PHASE_SCOPE",
    "ShadowQueryReport",
    "UpgradePhase",
    "evaluate_shadow",
    "phase_allowed",
]
