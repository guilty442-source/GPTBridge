"""Statistical Tolerance — warning/fail thresholds from multiple stable runs.

Instead of using a single measurement or an arbitrary fixed percentage,
tolerances are computed from N independent stable benchmark runs.

For each metric, the tolerance is:
    warning_threshold = mean + k_warn * std_dev
    fail_threshold = mean + k_fail * std_dev

Where k_warn and k_fail are configurable z-scores (default: 2 and 3,
corresponding to ~95% and ~99.7% confidence for normal distributions).

If fewer than 3 stable runs are available, the tolerance falls back to
a conservative fixed percentage (default 25% warn, 50% fail) to avoid
false positives from insufficient data.

Correctness/contract/resource safety always overrides performance:
if correctness fails, the result is FAIL regardless of tolerance.
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Sequence


@dataclass(frozen=True)
class MetricTolerance:
    """Tolerance for one metric."""
    metric_name: str
    mean: float
    std_dev: float
    warning_threshold: float
    fail_threshold: float
    sample_count: int
    method: str  # "statistical" or "fallback"
    # Direction: "lower_is_better" (latency, memory) or "higher_is_better" (throughput)
    direction: str = "lower_is_better"


@dataclass(frozen=True)
class ToleranceSet:
    """Set of tolerances for all metrics of one operation."""
    operation_id: str
    tolerances: dict[str, MetricTolerance]
    stable_run_count: int
    method: str  # "statistical" or "fallback"


# Default z-scores for warning/fail thresholds
DEFAULT_K_WARN = 2.0  # ~95% confidence
DEFAULT_K_FAIL = 3.0   # ~99.7% confidence

# Fallback thresholds when insufficient data
FALLBACK_WARN_PCT = 25.0  # 25% over baseline
FALLBACK_FAIL_PCT = 50.0  # 50% over baseline

# Minimum stable runs for statistical tolerance
MIN_STATISTICAL_RUNS = 3


# Metrics where lower is better (latency, memory, etc.)
LOWER_IS_BETTER = frozenset({
    "wall_p50", "wall_p95", "wall_p99",
    "cpu_p50", "cpu_p95", "cpu_p99",
    "peak_memory_p50", "retained_memory_p50",
    "sql_query_count", "sql_row_count", "sql_byte_count",
    "ts_python_crossings", "python_native_crossings", "python_csharp_crossings",
    "serialization_bytes", "serialization_count",
    "native_copy_bytes", "native_allocation_count",
    "queue_wait_ms", "model_wait_ms",
    "call_count", "allocation_count",
})

# Metrics where higher is better (throughput)
HIGHER_IS_BETTER = frozenset({
    "throughput_p50",
})


def compute_tolerance(
    metric_name: str,
    values: Sequence[float],
    *,
    k_warn: float = DEFAULT_K_WARN,
    k_fail: float = DEFAULT_K_FAIL,
) -> MetricTolerance:
    """Compute statistical tolerance for one metric from multiple runs.

    If fewer than MIN_STATISTICAL_RUNS values are provided, falls back
    to a conservative fixed-percentage tolerance.
    """
    clean_values = [float(v) for v in values if v is not None and not math.isnan(v) and not math.isinf(v)]

    if len(clean_values) < MIN_STATISTICAL_RUNS:
        # Fallback: use fixed percentage
        if not clean_values:
            baseline_value = 0.0
        else:
            baseline_value = clean_values[0]

        direction = "higher_is_better" if metric_name in HIGHER_IS_BETTER else "lower_is_better"

        if direction == "lower_is_better":
            warning_threshold = baseline_value * (1.0 + FALLBACK_WARN_PCT / 100.0)
            fail_threshold = baseline_value * (1.0 + FALLBACK_FAIL_PCT / 100.0)
        else:
            # Higher is better: threshold is BELOW baseline
            warning_threshold = baseline_value * (1.0 - FALLBACK_WARN_PCT / 100.0)
            fail_threshold = baseline_value * (1.0 - FALLBACK_FAIL_PCT / 100.0)

        return MetricTolerance(
            metric_name=metric_name,
            mean=baseline_value,
            std_dev=0.0,
            warning_threshold=warning_threshold,
            fail_threshold=fail_threshold,
            sample_count=len(clean_values),
            method="fallback",
            direction=direction,
        )

    # Statistical tolerance
    mean = statistics.mean(clean_values)
    std_dev = statistics.stdev(clean_values) if len(clean_values) > 1 else 0.0

    direction = "higher_is_better" if metric_name in HIGHER_IS_BETTER else "lower_is_better"

    if direction == "lower_is_better":
        warning_threshold = mean + k_warn * std_dev
        fail_threshold = mean + k_fail * std_dev
    else:
        # Higher is better: threshold is BELOW mean
        warning_threshold = mean - k_warn * std_dev
        fail_threshold = mean - k_fail * std_dev

    return MetricTolerance(
        metric_name=metric_name,
        mean=mean,
        std_dev=std_dev,
        warning_threshold=warning_threshold,
        fail_threshold=fail_threshold,
        sample_count=len(clean_values),
        method="statistical",
        direction=direction,
    )


def compute_tolerance_set(
    operation_id: str,
    metric_runs: dict[str, Sequence[float]],
    *,
    k_warn: float = DEFAULT_K_WARN,
    k_fail: float = DEFAULT_K_FAIL,
) -> ToleranceSet:
    """Compute tolerances for all metrics of one operation.

    ``metric_runs`` maps metric names to sequences of values from
    independent stable runs.
    """
    tolerances: dict[str, MetricTolerance] = {}
    methods: set[str] = set()

    for metric_name, values in metric_runs.items():
        tol = compute_tolerance(metric_name, values, k_warn=k_warn, k_fail=k_fail)
        tolerances[metric_name] = tol
        methods.add(tol.method)

    # If any metric used fallback, the whole set is "fallback"
    method = "statistical" if methods == {"statistical"} else "fallback"

    return ToleranceSet(
        operation_id=operation_id,
        tolerances=tolerances,
        stable_run_count=min(
            (len(v) for v in metric_runs.values()), default=0
        ),
        method=method,
    )


@dataclass(frozen=True)
class ToleranceCheckResult:
    """Result of checking a measurement against a tolerance."""
    metric_name: str
    actual: float
    mean: float
    std_dev: float
    warning_threshold: float
    fail_threshold: float
    status: str  # PASS, WARN, FAIL
    delta_pct: float
    message: str


def check_tolerance(
    tolerance: MetricTolerance,
    actual: float,
) -> ToleranceCheckResult:
    """Check a measurement against a tolerance.

    Returns:
        PASS: within normal range
        WARN: exceeds warning threshold but not fail
        FAIL: exceeds fail threshold
    """
    if tolerance.direction == "lower_is_better":
        if actual <= tolerance.warning_threshold:
            status = "PASS"
        elif actual <= tolerance.fail_threshold:
            status = "WARN"
        else:
            status = "FAIL"

        if tolerance.mean > 0:
            delta_pct = ((actual - tolerance.mean) / tolerance.mean) * 100.0
        else:
            delta_pct = 0.0 if actual == 0 else float("inf")
    else:
        # Higher is better
        if actual >= tolerance.warning_threshold:
            status = "PASS"
        elif actual >= tolerance.fail_threshold:
            status = "WARN"
        else:
            status = "FAIL"

        if tolerance.mean > 0:
            delta_pct = ((tolerance.mean - actual) / tolerance.mean) * 100.0
        else:
            delta_pct = 0.0 if actual == 0 else float("inf")

    if status == "PASS":
        message = f"{tolerance.metric_name} {actual:.4f} within tolerance (mean={tolerance.mean:.4f})"
    elif status == "WARN":
        message = (
            f"{tolerance.metric_name} {actual:.4f} exceeds warning threshold "
            f"{tolerance.warning_threshold:.4f} (mean={tolerance.mean:.4f}, "
            f"std={tolerance.std_dev:.4f})"
        )
    else:
        message = (
            f"{tolerance.metric_name} {actual:.4f} exceeds fail threshold "
            f"{tolerance.fail_threshold:.4f} (mean={tolerance.mean:.4f}, "
            f"std={tolerance.std_dev:.4f})"
        )

    return ToleranceCheckResult(
        metric_name=tolerance.metric_name,
        actual=actual,
        mean=tolerance.mean,
        std_dev=tolerance.std_dev,
        warning_threshold=tolerance.warning_threshold,
        fail_threshold=tolerance.fail_threshold,
        status=status,
        delta_pct=round(delta_pct, 2),
        message=message,
    )


def check_all_tolerances(
    tolerance_set: ToleranceSet,
    measurements: dict[str, float],
) -> list[ToleranceCheckResult]:
    """Check all measurements against their tolerances."""
    results: list[ToleranceCheckResult] = []
    for metric_name, actual in measurements.items():
        tol = tolerance_set.tolerances.get(metric_name)
        if tol is None:
            # No tolerance defined for this metric; skip
            continue
        results.append(check_tolerance(tol, actual))
    return results


__all__ = [
    "MetricTolerance",
    "ToleranceSet",
    "ToleranceCheckResult",
    "compute_tolerance",
    "compute_tolerance_set",
    "check_tolerance",
    "check_all_tolerances",
    "LOWER_IS_BETTER",
    "HIGHER_IS_BETTER",
    "DEFAULT_K_WARN",
    "DEFAULT_K_FAIL",
    "MIN_STATISTICAL_RUNS",
    "FALLBACK_WARN_PCT",
    "FALLBACK_FAIL_PCT",
]
