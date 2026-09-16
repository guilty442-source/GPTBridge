"""Native Candidate — classification and Python fallback preservation.

Classifies a benchmarked capability into one of:
    KEEP_PYTHON        — no profile evidence of a bottleneck; keep Python.
    OPTIMIZE_PYTHON    — bottleneck found but solvable in Python
                         (batching, SQL pushdown, boundary-copy reduction).
    EXPERIMENTAL_NATIVE — CPU/memory bottleneck found; native path exists
                          but not yet parity/budget/fallback complete.
    NATIVE_CANDIDATE   — CPU/memory bottleneck + native path + parity +
                         budget + fallback all pass; ready for the existing
                         Native Promotion Gate (A357).

Python fallback is ALWAYS preserved (A219).  Only NATIVE_CANDIDATE is
handed to the existing Native Promotion Gate; the other three stay in
Python.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

KEEP_PYTHON = "KEEP_PYTHON"
OPTIMIZE_PYTHON = "OPTIMIZE_PYTHON"
EXPERIMENTAL_NATIVE = "EXPERIMENTAL_NATIVE"
NATIVE_CANDIDATE = "NATIVE_CANDIDATE"

# Only these bottleneck classes are eligible for native promotion.
_NATIVE_ELIGIBLE_CLASSES = frozenset({"cpu_bound", "memory_copy_bound"})


@dataclass(frozen=True)
class NativeCandidate:
    """A benchmarked capability's native-promotion classification."""
    capability_id: str
    verdict: str
    bottleneck_class: str
    python_wall_p50: float
    native_wall_p50: float | None
    speedup: float | None
    parity_passed: bool
    fallback_preserved: bool
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PromotionVerdict:
    """Verdict for the existing Native Promotion Gate (A357)."""
    capability_id: str
    verdict: str  # PASS / WARN / FAIL
    reason: str
    evidence: dict[str, Any]


def classify_candidate(
    capability_id: str,
    bottleneck_class: str,
    python_wall_p50: float,
    native_wall_p50: float | None,
    *,
    parity_passed: bool = True,
    fallback_preserved: bool = True,
    minimum_meaningful_improvement: float = 1.5,
    budget_latency_ms: float | None = None,
    budget_memory_bytes: int | None = None,
    native_peak_memory_bytes: int | None = None,
    extra_evidence: dict[str, Any] | None = None,
) -> NativeCandidate:
    """Classify a benchmarked capability.

    Decision tree:
        1. bottleneck not cpu/memory -> KEEP_PYTHON or OPTIMIZE_PYTHON.
        2. bottleneck is cpu/memory but no native path -> OPTIMIZE_PYTHON
           (try Python optimizations first: batching, copy reduction).
        3. native path exists but parity/fallback incomplete ->
           EXPERIMENTAL_NATIVE.
        4. native path + parity + fallback + speedup >= threshold + budget
           -> NATIVE_CANDIDATE (hand to existing Native Promotion Gate).
    """
    evidence: dict[str, Any] = {
        "bottleneck_class": bottleneck_class,
        "python_wall_p50": python_wall_p50,
        "native_wall_p50": native_wall_p50,
        "parity_passed": parity_passed,
        "fallback_preserved": fallback_preserved,
        "minimum_meaningful_improvement": minimum_meaningful_improvement,
    }
    if extra_evidence:
        evidence.update(extra_evidence)

    speedup: float | None = None
    if native_wall_p50 is not None and native_wall_p50 > 0:
        speedup = python_wall_p50 / native_wall_p50
        evidence["speedup"] = round(speedup, 3)

    # 1. Non-native-eligible bottleneck
    if bottleneck_class not in _NATIVE_ELIGIBLE_CLASSES:
        verdict = OPTIMIZE_PYTHON if bottleneck_class != "unknown" else KEEP_PYTHON
        return NativeCandidate(
            capability_id=capability_id,
            verdict=verdict,
            bottleneck_class=bottleneck_class,
            python_wall_p50=python_wall_p50,
            native_wall_p50=native_wall_p50,
            speedup=speedup,
            parity_passed=parity_passed,
            fallback_preserved=fallback_preserved,
            evidence=evidence,
        )

    # 2. CPU/memory bottleneck but no native path
    if native_wall_p50 is None:
        return NativeCandidate(
            capability_id=capability_id,
            verdict=OPTIMIZE_PYTHON,
            bottleneck_class=bottleneck_class,
            python_wall_p50=python_wall_p50,
            native_wall_p50=None,
            speedup=None,
            parity_passed=parity_passed,
            fallback_preserved=fallback_preserved,
            evidence=evidence,
        )

    # 3. Native path exists but parity or fallback incomplete
    if not parity_passed or not fallback_preserved:
        return NativeCandidate(
            capability_id=capability_id,
            verdict=EXPERIMENTAL_NATIVE,
            bottleneck_class=bottleneck_class,
            python_wall_p50=python_wall_p50,
            native_wall_p50=native_wall_p50,
            speedup=speedup,
            parity_passed=parity_passed,
            fallback_preserved=fallback_preserved,
            evidence=evidence,
        )

    # 4. Budget checks
    if budget_latency_ms is not None:
        native_latency_ms = native_wall_p50 * 1000
        if native_latency_ms > budget_latency_ms:
            evidence["budget_violation"] = "latency"
            evidence["native_latency_ms"] = native_latency_ms
            evidence["budget_latency_ms"] = budget_latency_ms
            return NativeCandidate(
                capability_id=capability_id,
                verdict=EXPERIMENTAL_NATIVE,
                bottleneck_class=bottleneck_class,
                python_wall_p50=python_wall_p50,
                native_wall_p50=native_wall_p50,
                speedup=speedup,
                parity_passed=parity_passed,
                fallback_preserved=fallback_preserved,
                evidence=evidence,
            )
    if budget_memory_bytes is not None and native_peak_memory_bytes is not None:
        if native_peak_memory_bytes > budget_memory_bytes:
            evidence["budget_violation"] = "memory"
            evidence["native_peak_memory_bytes"] = native_peak_memory_bytes
            evidence["budget_memory_bytes"] = budget_memory_bytes
            return NativeCandidate(
                capability_id=capability_id,
                verdict=EXPERIMENTAL_NATIVE,
                bottleneck_class=bottleneck_class,
                python_wall_p50=python_wall_p50,
                native_wall_p50=native_wall_p50,
                speedup=speedup,
                parity_passed=parity_passed,
                fallback_preserved=fallback_preserved,
                evidence=evidence,
            )

    # 5. Speedup threshold
    if speedup is None or speedup < minimum_meaningful_improvement:
        evidence["reason"] = (
            f"speedup {speedup} below minimum_meaningful_improvement "
            f"{minimum_meaningful_improvement}"
        )
        return NativeCandidate(
            capability_id=capability_id,
            verdict=EXPERIMENTAL_NATIVE,
            bottleneck_class=bottleneck_class,
            python_wall_p50=python_wall_p50,
            native_wall_p50=native_wall_p50,
            speedup=speedup,
            parity_passed=parity_passed,
            fallback_preserved=fallback_preserved,
            evidence=evidence,
        )

    # 6. All checks pass -> NATIVE_CANDIDATE
    return NativeCandidate(
        capability_id=capability_id,
        verdict=NATIVE_CANDIDATE,
        bottleneck_class=bottleneck_class,
        python_wall_p50=python_wall_p50,
        native_wall_p50=native_wall_p50,
        speedup=speedup,
        parity_passed=parity_passed,
        fallback_preserved=fallback_preserved,
        evidence=evidence,
    )


def to_promotion_verdict(candidate: NativeCandidate) -> PromotionVerdict:
    """Convert a NATIVE_CANDIDATE into a verdict for the existing
    Native Promotion Gate (A357).  Non-NATIVE_CANDIDATE verdicts produce
    a WARN (experimental) or PASS (keep Python) -- they do NOT enter the
    production native path.
    """
    if candidate.verdict == NATIVE_CANDIDATE:
        return PromotionVerdict(
            capability_id=candidate.capability_id,
            verdict="PASS",
            reason=(
                f"CPU/memory bottleneck + native parity + fallback + "
                f"speedup {candidate.speedup}x + budget pass"
            ),
            evidence=candidate.evidence,
        )
    if candidate.verdict == EXPERIMENTAL_NATIVE:
        return PromotionVerdict(
            capability_id=candidate.capability_id,
            verdict="WARN",
            reason="experimental only; not reachable from production",
            evidence=candidate.evidence,
        )
    return PromotionVerdict(
        capability_id=candidate.capability_id,
        verdict="PASS",
        reason=f"keep Python ({candidate.verdict})",
        evidence=candidate.evidence,
    )


__all__ = [
    "NativeCandidate",
    "PromotionVerdict",
    "classify_candidate",
    "to_promotion_verdict",
    "KEEP_PYTHON",
    "OPTIMIZE_PYTHON",
    "EXPERIMENTAL_NATIVE",
    "NATIVE_CANDIDATE",
]
