"""Regression — versioned baseline comparison and regression evidence.

Compares two baseline records for the same capability and detects
regressions in p50/p95/p99 wall time, peak memory, and allocation count.
Produces evidence suitable for the existing Native Promotion Gate and
for commit-time regression checks (without adding a new gate).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .baseline import BaselineRecord


@dataclass(frozen=True)
class RegressionEvidence:
    """Comparison evidence between two baselines."""
    capability_id: str
    before_id: str
    after_id: str
    wall_p50_delta_pct: float
    wall_p95_delta_pct: float
    wall_p99_delta_pct: float
    peak_memory_delta_pct: float
    allocation_delta_pct: float
    is_regression: bool
    regression_reasons: tuple[str, ...]


def _pct(before: float, after: float) -> float:
    if before == 0:
        return 0.0 if after == 0 else float("inf")
    return ((after - before) / before) * 100.0


def compare_baselines(
    before: BaselineRecord,
    after: BaselineRecord,
    *,
    regression_threshold_pct: float = 10.0,
) -> RegressionEvidence:
    """Compare two baselines and detect regressions.

    A regression is any metric that worsened by more than
    ``regression_threshold_pct`` (default 10%).
    """
    pm = before.python_metrics
    am = after.python_metrics

    wall_p50_delta = _pct(
        float(pm.get("wall_p50", 0)), float(am.get("wall_p50", 0))
    )
    wall_p95_delta = _pct(
        float(pm.get("wall_p95", 0)), float(am.get("wall_p95", 0))
    )
    wall_p99_delta = _pct(
        float(pm.get("wall_p99", 0)), float(am.get("wall_p99", 0))
    )
    mem_delta = _pct(
        float(pm.get("peak_memory_p50", 0)), float(am.get("peak_memory_p50", 0))
    )
    alloc_delta = _pct(
        float(pm.get("allocation_p50", 0)), float(am.get("allocation_p50", 0))
    )

    reasons: list[str] = []
    if wall_p50_delta > regression_threshold_pct:
        reasons.append(f"wall_p50 regressed {wall_p50_delta:.1f}%")
    if wall_p95_delta > regression_threshold_pct:
        reasons.append(f"wall_p95 regressed {wall_p95_delta:.1f}%")
    if wall_p99_delta > regression_threshold_pct:
        reasons.append(f"wall_p99 regressed {wall_p99_delta:.1f}%")
    if mem_delta > regression_threshold_pct:
        reasons.append(f"peak_memory regressed {mem_delta:.1f}%")
    if alloc_delta > regression_threshold_pct:
        reasons.append(f"allocation regressed {alloc_delta:.1f}%")

    return RegressionEvidence(
        capability_id=before.capability_id,
        before_id=before.baseline_id,
        after_id=after.baseline_id,
        wall_p50_delta_pct=round(wall_p50_delta, 2),
        wall_p95_delta_pct=round(wall_p95_delta, 2),
        wall_p99_delta_pct=round(wall_p99_delta, 2),
        peak_memory_delta_pct=round(mem_delta, 2),
        allocation_delta_pct=round(alloc_delta, 2),
        is_regression=bool(reasons),
        regression_reasons=tuple(reasons),
    )


def detect_regression(
    before: BaselineRecord,
    after: BaselineRecord,
    *,
    regression_threshold_pct: float = 10.0,
) -> bool:
    """Return True if ``after`` regressed vs ``before``."""
    return compare_baselines(
        before, after, regression_threshold_pct=regression_threshold_pct
    ).is_regression


__all__ = [
    "RegressionEvidence",
    "compare_baselines",
    "detect_regression",
]
