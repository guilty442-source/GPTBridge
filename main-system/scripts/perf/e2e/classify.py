"""Path classification + native-optimization verdict.

Each benchmarked path lands in exactly one class:

    HEALTHY                no dominant internal cost
    PYTHON_OPTIMIZATION    Python phases dominate the critical path
    DATA_IO_OPTIMIZATION   SQL / transport dominates
    NATIVE_OPTIMIZATION    native queue+boundary+compute dominate AND the
                           native leg is actually on the critical path

Native work may only continue when it is measurably on the critical path
with a sufficient share — never on intuition.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .benchmark import PathBenchmark
from .spans import Phase

# Governed thresholds — a phase group must hold this share of the request
# critical path before it may claim the classification.
NATIVE_DOMINANCE = 0.30
DATA_IO_DOMINANCE = 0.40
PYTHON_DOMINANCE = 0.40
# External model wait dominating is HEALTHY — nothing internal to fix.
EXTERNAL_WAIT_HEALTHY = 0.50
# Native must move user-visible E2E latency by at least this much to keep
# the optimization line open.
NATIVE_MIN_E2E_GAIN = 0.05

_NATIVE_PHASES = {
    Phase.NATIVE_QUEUE.value,
    Phase.NATIVE_BOUNDARY.value,
    Phase.NATIVE_COMPUTE.value,
}
_DATA_IO_PHASES = {Phase.SQL_ROUNDTRIP.value, Phase.TRANSPORT.value}
_PYTHON_PHASES = {
    Phase.PY_VALIDATION.value,
    Phase.PY_ORCHESTRATION.value,
    Phase.SERIALIZATION.value,
    Phase.RESULT_PROCESSING.value,
}


class PathClass(str, Enum):
    HEALTHY = "HEALTHY"
    PYTHON_OPTIMIZATION = "PYTHON_OPTIMIZATION"
    DATA_IO_OPTIMIZATION = "DATA_IO_OPTIMIZATION"
    NATIVE_OPTIMIZATION = "NATIVE_OPTIMIZATION"


def _share(bench: PathBenchmark, phases: set[str]) -> float:
    return sum(bench.phase_shares.get(p, 0.0) for p in phases)


def native_share(bench: PathBenchmark) -> float:
    return _share(bench, _NATIVE_PHASES)


def classify_path(bench: PathBenchmark) -> PathClass:
    shares = bench.phase_shares
    if shares.get(Phase.MODEL_WAIT.value, 0.0) >= EXTERNAL_WAIT_HEALTHY:
        return PathClass.HEALTHY  # dominated by external model wait
    if _share(bench, _NATIVE_PHASES) >= NATIVE_DOMINANCE:
        return PathClass.NATIVE_OPTIMIZATION
    if _share(bench, _DATA_IO_PHASES) >= DATA_IO_DOMINANCE:
        return PathClass.DATA_IO_OPTIMIZATION
    if _share(bench, _PYTHON_PHASES) >= PYTHON_DOMINANCE:
        return PathClass.PYTHON_OPTIMIZATION
    return PathClass.HEALTHY


@dataclass(frozen=True)
class NativeVerdict:
    """Before/after native comparison on user-visible E2E latency."""
    path_name: str
    before_p50_ms: float
    after_p50_ms: float
    e2e_delta_ms: float
    e2e_gain_ratio: float
    native_on_critical_path: bool
    native_share_before: float
    justified: bool
    reason: str


def compare_native(
    before: PathBenchmark, after: PathBenchmark
) -> NativeVerdict:
    """Native optimization may only continue when it was on the critical
    path AND moved user-visible E2E latency."""
    share = native_share(before)
    on_path = share >= NATIVE_DOMINANCE
    delta = before.p50_ms - after.p50_ms
    gain = delta / before.p50_ms if before.p50_ms > 0 else 0.0
    justified = on_path and gain >= NATIVE_MIN_E2E_GAIN
    if not on_path:
        reason = (
            f"native share {share:.1%} below {NATIVE_DOMINANCE:.0%} "
            "critical-path threshold — optimization not justified"
        )
    elif gain < NATIVE_MIN_E2E_GAIN:
        reason = (
            f"E2E gain {gain:.1%} below {NATIVE_MIN_E2E_GAIN:.0%} — "
            "native work did not move user-visible latency"
        )
    else:
        reason = (
            f"native share {share:.1%} and E2E gain {gain:.1%} — "
            "optimization justified"
        )
    return NativeVerdict(
        path_name=before.path_name,
        before_p50_ms=before.p50_ms,
        after_p50_ms=after.p50_ms,
        e2e_delta_ms=delta,
        e2e_gain_ratio=gain,
        native_on_critical_path=on_path,
        native_share_before=share,
        justified=justified,
        reason=reason,
    )


__all__ = [
    "PathClass",
    "NativeVerdict",
    "classify_path",
    "compare_native",
    "native_share",
    "NATIVE_DOMINANCE",
    "DATA_IO_DOMINANCE",
    "PYTHON_DOMINANCE",
]
