"""Benchmark + Security gates — deployment conditions, not opinions.

Embedding upgrades are judged by a fixed benchmark, never by
comparing raw cosine across embedding spaces:

    old 0.86 vs new 0.79 proves nothing; Recall@K / MRR / NDCG /
    zero-result / wrong-hit / latency vs acceptance thresholds is
    the gate.

Security benchmark outranks answer quality — any single failure is
DEPLOYMENT_BLOCKED.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class GateVerdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    DEPLOYMENT_BLOCKED = "DEPLOYMENT_BLOCKED"


@dataclass(frozen=True, slots=True)
class AcceptanceThresholds:
    recall_at_5: float = 0.70
    recall_at_10: float = 0.80
    mrr: float = 0.50
    ndcg_at_10: float = 0.60
    max_zero_result_rate: float = 0.10
    max_wrong_hit_rate: float = 0.05
    max_p95_latency_ms: float = 500.0
    min_citation_accuracy: float = 0.95


@dataclass(frozen=True, slots=True)
class BenchmarkOutcome:
    recall_at_5: float
    recall_at_10: float
    mrr: float
    ndcg_at_10: float
    zero_result_rate: float
    wrong_hit_rate: float
    p95_latency_ms: float
    citation_accuracy: float = 1.0


@dataclass(frozen=True, slots=True)
class SecurityProbe:
    """One security check; a single failure blocks deployment."""

    name: str
    passed: bool
    detail: str = ""


# The required probe set — all must run; absence of a probe is itself
# a failure (untested surface is not a pass).
REQUIRED_SECURITY_PROBES: tuple[str, ...] = (
    "cross-module-leakage",
    "unauthorized-retrieval",
    "tombstoned-resource-searchable",
    "physical-path-in-payload",
    "content-in-qdrant-payload",
    "degraded-mismarked-canonical",
)


def benchmark_gate(
    outcome: BenchmarkOutcome,
    thresholds: AcceptanceThresholds = AcceptanceThresholds(),
) -> tuple[GateVerdict, tuple[str, ...]]:
    """new generation benchmark >= acceptance threshold, else FAIL."""
    failures: list[str] = []
    if outcome.recall_at_5 < thresholds.recall_at_5:
        failures.append(f"recall@5 {outcome.recall_at_5}<{thresholds.recall_at_5}")
    if outcome.recall_at_10 < thresholds.recall_at_10:
        failures.append(f"recall@10 {outcome.recall_at_10}<{thresholds.recall_at_10}")
    if outcome.mrr < thresholds.mrr:
        failures.append(f"mrr {outcome.mrr}<{thresholds.mrr}")
    if outcome.ndcg_at_10 < thresholds.ndcg_at_10:
        failures.append(f"ndcg@10 {outcome.ndcg_at_10}<{thresholds.ndcg_at_10}")
    if outcome.zero_result_rate > thresholds.max_zero_result_rate:
        failures.append(f"zero-result {outcome.zero_result_rate}>{thresholds.max_zero_result_rate}")
    if outcome.wrong_hit_rate > thresholds.max_wrong_hit_rate:
        failures.append(f"wrong-hit {outcome.wrong_hit_rate}>{thresholds.max_wrong_hit_rate}")
    if outcome.p95_latency_ms > thresholds.max_p95_latency_ms:
        failures.append(f"p95 {outcome.p95_latency_ms}>{thresholds.max_p95_latency_ms}")
    if outcome.citation_accuracy < thresholds.min_citation_accuracy:
        failures.append(f"citation {outcome.citation_accuracy}<{thresholds.min_citation_accuracy}")
    return (
        (GateVerdict.PASS, ()) if not failures
        else (GateVerdict.FAIL, tuple(failures))
    )


def security_gate(probes: list[SecurityProbe]) -> tuple[GateVerdict, tuple[str, ...]]:
    """All required probes present and passing -> PASS; otherwise
    DEPLOYMENT_BLOCKED."""
    failures: list[str] = []
    seen = {p.name for p in probes}
    for required in REQUIRED_SECURITY_PROBES:
        if required not in seen:
            failures.append(f"missing-probe:{required}")
    for p in probes:
        if not p.passed:
            failures.append(f"probe-failed:{p.name}:{p.detail}")
    return (
        (GateVerdict.PASS, ()) if not failures
        else (GateVerdict.DEPLOYMENT_BLOCKED, tuple(failures))
    )


def deployment_gate(
    outcome: BenchmarkOutcome,
    probes: list[SecurityProbe],
    thresholds: AcceptanceThresholds = AcceptanceThresholds(),
) -> tuple[GateVerdict, tuple[str, ...]]:
    """Combined gate — security failures dominate benchmark ones."""
    s_verdict, s_fails = security_gate(probes)
    b_verdict, b_fails = benchmark_gate(outcome, thresholds)
    if s_verdict is GateVerdict.DEPLOYMENT_BLOCKED:
        return GateVerdict.DEPLOYMENT_BLOCKED, s_fails + b_fails
    if b_verdict is GateVerdict.FAIL:
        return GateVerdict.FAIL, b_fails
    return GateVerdict.PASS, ()


__all__ = [
    "AcceptanceThresholds",
    "BenchmarkOutcome",
    "GateVerdict",
    "REQUIRED_SECURITY_PROBES",
    "SecurityProbe",
    "benchmark_gate",
    "deployment_gate",
    "security_gate",
]
