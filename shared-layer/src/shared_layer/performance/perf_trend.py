"""Performance Trend — historical trend and regression attribution.

Tracks baseline history per operation and produces regression attribution:
    - operation, metric, baseline/current/delta
    - dominant changed spans (which metrics changed the most)
    - corresponding Python/SQL-I-O/native boundary

This helps answer: "what changed, where, and by how much?"
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .perf_baseline import PerformanceBaselineRecord
from .regression import _pct


@dataclass(frozen=True)
class MetricTrend:
    """Trend for one metric across baseline history."""
    metric_name: str
    values: tuple[float, ...]     # chronological values
    baseline_ids: tuple[str, ...] # corresponding baseline IDs
    direction: str                # "improving" / "regressing" / "stable" / "unknown"
    total_delta_pct: float        # first to last
    recent_delta_pct: float       # second-to-last to last


@dataclass(frozen=True)
class RegressionAttribution:
    """Attribution for a regression: what changed, where, by how much."""
    operation_id: str
    metric_name: str
    baseline_value: float
    current_value: float
    delta_pct: float
    status: str                   # PASS / WARN / FAIL
    # Dominant changed span: which boundary is responsible
    dominant_boundary: str        # "python" / "sql" / "native" / "io" / "model" / "unknown"
    dominant_span: str            # e.g. "vector.cpp:dot" or "store_helpers:encode_json"
    message: str


@dataclass(frozen=True)
class OperationTrend:
    """Full trend for one operation across baseline history."""
    operation_id: str
    baseline_count: int
    metric_trends: dict[str, MetricTrend]
    overall_direction: str        # "improving" / "regressing" / "stable" / "unknown"
    regressions: tuple[RegressionAttribution, ...]


def _classify_direction(total_delta: float, recent_delta: float) -> str:
    """Classify the direction of a metric trend."""
    threshold = 5.0  # 5% change threshold

    if abs(total_delta) < threshold and abs(recent_delta) < threshold:
        return "stable"
    elif total_delta < 0 and recent_delta < 0:
        return "improving"
    elif total_delta > 0 and recent_delta > 0:
        return "regressing"
    elif recent_delta > 0:
        return "regressing"
    elif recent_delta < 0:
        return "improving"
    else:
        return "unknown"


def compute_metric_trend(
    metric_name: str,
    history: list[PerformanceBaselineRecord],
) -> MetricTrend:
    """Compute the trend for one metric across baseline history."""
    values: list[float] = []
    baseline_ids: list[str] = []

    for record in history:
        val = record.metrics.get(metric_name)
        if val is not None and not math.isnan(float(val)) and not math.isinf(float(val)):
            values.append(float(val))
            baseline_ids.append(record.baseline_id)

    if len(values) < 2:
        return MetricTrend(
            metric_name=metric_name,
            values=tuple(values),
            baseline_ids=tuple(baseline_ids),
            direction="unknown",
            total_delta_pct=0.0,
            recent_delta_pct=0.0,
        )

    total_delta = _pct(values[0], values[-1])
    recent_delta = _pct(values[-2], values[-1])
    direction = _classify_direction(total_delta, recent_delta)

    return MetricTrend(
        metric_name=metric_name,
        values=tuple(values),
        baseline_ids=tuple(baseline_ids),
        direction=direction,
        total_delta_pct=round(total_delta, 2),
        recent_delta_pct=round(recent_delta, 2),
    )


# Map metrics to their dominant boundary
METRIC_BOUNDARY_MAP: dict[str, str] = {
    "wall_p50": "python",
    "wall_p95": "python",
    "wall_p99": "python",
    "cpu_p50": "python",
    "cpu_p95": "python",
    "cpu_p99": "python",
    "throughput_p50": "python",
    "peak_memory_p50": "python",
    "retained_memory_p50": "python",
    "sql_query_count": "sql",
    "sql_row_count": "sql",
    "sql_byte_count": "sql",
    "ts_python_crossings": "ts_python",
    "python_native_crossings": "native",
    "python_csharp_crossings": "csharp",
    "serialization_bytes": "python",
    "serialization_count": "python",
    "native_copy_bytes": "native",
    "native_allocation_count": "native",
    "queue_wait_ms": "model",
    "model_wait_ms": "model",
    "call_count": "python",
    "allocation_count": "python",
}


def attribute_regression(
    operation_id: str,
    metric_name: str,
    baseline_value: float,
    current_value: float,
    status: str,
) -> RegressionAttribution:
    """Attribute a regression to a dominant boundary and span."""
    delta = _pct(baseline_value, current_value)
    boundary = METRIC_BOUNDARY_MAP.get(metric_name, "unknown")

    # Dominant span: best-effort guess from metric name
    if "sql" in metric_name:
        span = "sql:query_execution"
    elif "native" in metric_name or "crossing" in metric_name:
        span = "native:boundary_crossing"
    elif "serialization" in metric_name:
        span = "python:json_encode_decode"
    elif "model" in metric_name or "queue" in metric_name:
        span = "model:wait"
    elif "memory" in metric_name:
        span = "python:memory_allocation"
    elif "wall" in metric_name or "cpu" in metric_name:
        span = "python:compute"
    else:
        span = "unknown"

    message = (
        f"{operation_id}.{metric_name}: {baseline_value:.4f} -> "
        f"{current_value:.4f} ({delta:+.1f}%) [{status}] "
        f"dominant_boundary={boundary} span={span}"
    )

    return RegressionAttribution(
        operation_id=operation_id,
        metric_name=metric_name,
        baseline_value=baseline_value,
        current_value=current_value,
        delta_pct=round(delta, 2),
        status=status,
        dominant_boundary=boundary,
        dominant_span=span,
        message=message,
    )


def compute_operation_trend(
    operation_id: str,
    history: list[PerformanceBaselineRecord],
    metric_names: list[str] | None = None,
) -> OperationTrend:
    """Compute the full trend for one operation."""
    if not history:
        return OperationTrend(
            operation_id=operation_id,
            baseline_count=0,
            metric_trends={},
            overall_direction="unknown",
            regressions=(),
        )

    # Determine which metrics to track
    if metric_names is None:
        # Use all metrics from the latest baseline
        metric_names = list(history[-1].metrics.keys())

    metric_trends: dict[str, MetricTrend] = {}
    for name in metric_names:
        metric_trends[name] = compute_metric_trend(name, history)

    # Overall direction: majority vote
    directions = [t.direction for t in metric_trends.values()]
    improving = directions.count("improving")
    regressing = directions.count("regressing")
    stable = directions.count("stable")

    if stable > improving and stable > regressing:
        overall = "stable"
    elif regressing > improving:
        overall = "regressing"
    elif improving > regressing:
        overall = "improving"
    else:
        overall = "unknown"

    # Identify regressions in the latest comparison
    regressions: list[RegressionAttribution] = []
    if len(history) >= 2:
        prev = history[-2]
        curr = history[-1]
        for name in metric_names:
            prev_val = float(prev.metrics.get(name, 0))
            curr_val = float(curr.metrics.get(name, 0))
            if prev_val == 0 and curr_val == 0:
                continue
            delta = _pct(prev_val, curr_val)
            # For lower-is-better metrics, positive delta = regression
            if name not in ("throughput_p50",):
                if delta > 5.0:  # 5% regression threshold
                    regressions.append(attribute_regression(
                        operation_id, name, prev_val, curr_val, "WARN"
                    ))
            else:
                # Higher is better: negative delta = regression
                if delta < -5.0:
                    regressions.append(attribute_regression(
                        operation_id, name, prev_val, curr_val, "WARN"
                    ))

    return OperationTrend(
        operation_id=operation_id,
        baseline_count=len(history),
        metric_trends=metric_trends,
        overall_direction=overall,
        regressions=tuple(regressions),
    )


__all__ = [
    "MetricTrend",
    "RegressionAttribution",
    "OperationTrend",
    "compute_metric_trend",
    "compute_operation_trend",
    "attribute_regression",
    "METRIC_BOUNDARY_MAP",
]
