"""Performance Stability — PERFORMANCE_STABLE marking.

An operation is marked PERFORMANCE_STABLE when ALL of the following
budgets are met:
    - E2E latency (wall_p50, wall_p95, wall_p99)
    - memory (peak_memory_p50, retained_memory_p50)
    - boundary crossings (python_native_crossings, ts_python_crossings)
    - SQL round-trip (sql_query_count)
    - resource budget (cpu_p50, allocation_count)

When all budgets are met and the trend is stable or improving, the
operation is marked PERFORMANCE_STABLE.  This signals that further
micro-optimization without new evidence is not warranted.

Correctness/contract/resource safety always overrides stability:
if correctness fails, the operation is UNSTABLE regardless of budgets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .perf_baseline import PerformanceBaselineRecord
from .perf_trend import OperationTrend


# Budget thresholds for stability marking
# These are the maximum values for an operation to be considered stable.
# They are NOT governance gates — they are evidence thresholds.
STABILITY_BUDGETS: dict[str, dict[str, float]] = {
    # Latency budgets (seconds)
    "wall_p50": {"max": 10.0, "description": "E2E p50 latency"},
    "wall_p95": {"max": 50.0, "description": "E2E p95 latency"},
    "wall_p99": {"max": 100.0, "description": "E2E p99 latency"},
    # Memory budgets (bytes)
    "peak_memory_p50": {"max": 100 * 1024 * 1024, "description": "Peak memory"},
    "retained_memory_p50": {"max": 50 * 1024 * 1024, "description": "Retained memory"},
    # Boundary crossing budgets
    "python_native_crossings": {"max": 100, "description": "Python-native crossings"},
    "ts_python_crossings": {"max": 50, "description": "TS-Python crossings"},
    # SQL round-trip budget
    "sql_query_count": {"max": 20, "description": "SQL queries per op"},
    # Resource budgets
    "cpu_p50": {"max": 10.0, "description": "CPU time p50"},
    "allocation_count": {"max": 1000000, "description": "Allocation count"},
}


@dataclass(frozen=True)
class StabilityCheck:
    """Result of checking one stability budget."""
    metric_name: str
    actual: float
    budget_max: float
    passed: bool
    message: str


@dataclass(frozen=True)
class StabilityResult:
    """Result of stability assessment for one operation."""
    operation_id: str
    baseline_id: str
    stability: str  # "PERFORMANCE_STABLE" / "UNSTABLE" / "UNKNOWN"
    checks: tuple[StabilityCheck, ...]
    trend_direction: str
    all_budgets_met: bool
    correctness_passed: bool
    message: str


def check_stability_budgets(
    metrics: dict[str, Any],
    budgets: dict[str, dict[str, float]] | None = None,
) -> list[StabilityCheck]:
    """Check all stability budgets for a set of metrics."""
    budgets = budgets or STABILITY_BUDGETS
    checks: list[StabilityCheck] = []

    for metric_name, budget in budgets.items():
        actual = float(metrics.get(metric_name, 0))
        budget_max = budget["max"]
        passed = actual <= budget_max

        if passed:
            message = f"{metric_name} {actual:.4f} <= budget {budget_max:.4f}"
        else:
            message = f"{metric_name} {actual:.4f} > budget {budget_max:.4f}"

        checks.append(StabilityCheck(
            metric_name=metric_name,
            actual=actual,
            budget_max=budget_max,
            passed=passed,
            message=message,
        ))

    return checks


def assess_stability(
    baseline: PerformanceBaselineRecord,
    trend: OperationTrend | None = None,
    *,
    correctness_passed: bool = True,
    budgets: dict[str, dict[str, float]] | None = None,
) -> StabilityResult:
    """Assess whether an operation is PERFORMANCE_STABLE.

    An operation is PERFORMANCE_STABLE when:
        1. All stability budgets are met
        2. The trend is stable or improving (or unknown with no regressions)
        3. Correctness/contract/resource safety passes
    """
    checks = check_stability_budgets(baseline.metrics, budgets)
    all_budgets_met = all(c.passed for c in checks)

    trend_direction = "unknown"
    if trend is not None:
        trend_direction = trend.overall_direction

    # Correctness always overrides
    if not correctness_passed:
        return StabilityResult(
            operation_id=baseline.operation_id,
            baseline_id=baseline.baseline_id,
            stability="UNSTABLE",
            checks=tuple(checks),
            trend_direction=trend_direction,
            all_budgets_met=all_budgets_met,
            correctness_passed=False,
            message="UNSTABLE: correctness/contract check failed (overrides all)",
        )

    # Check budgets
    if not all_budgets_met:
        failed = [c.metric_name for c in checks if not c.passed]
        return StabilityResult(
            operation_id=baseline.operation_id,
            baseline_id=baseline.baseline_id,
            stability="UNSTABLE",
            checks=tuple(checks),
            trend_direction=trend_direction,
            all_budgets_met=False,
            correctness_passed=True,
            message=f"UNSTABLE: budgets not met: {', '.join(failed)}",
        )

    # Check trend
    if trend_direction == "regressing":
        return StabilityResult(
            operation_id=baseline.operation_id,
            baseline_id=baseline.baseline_id,
            stability="UNSTABLE",
            checks=tuple(checks),
            trend_direction=trend_direction,
            all_budgets_met=True,
            correctness_passed=True,
            message="UNSTABLE: trend is regressing despite budgets met",
        )

    # All checks passed
    return StabilityResult(
        operation_id=baseline.operation_id,
        baseline_id=baseline.baseline_id,
        stability="PERFORMANCE_STABLE",
        checks=tuple(checks),
        trend_direction=trend_direction,
        all_budgets_met=True,
        correctness_passed=True,
        message=(
            "PERFORMANCE_STABLE: all budgets met, trend stable/improving, "
            "correctness passed.  Stop evidence-free micro-optimization."
        ),
    )


__all__ = [
    "StabilityCheck",
    "StabilityResult",
    "STABILITY_BUDGETS",
    "check_stability_budgets",
    "assess_stability",
]
