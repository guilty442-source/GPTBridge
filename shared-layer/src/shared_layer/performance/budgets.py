"""Performance Budgets — Python runtime performance budgets V1.

Defines per-capability performance budgets for the Python runtime path.
A budget is a soft limit: exceeding it triggers a WARN (not a FAIL) in
the regression benchmark, and a FAIL only if the budget is exceeded by
more than the tolerance margin.

Budgets are NOT governance gates (no new gate per V1 constraint).  They
are evidence thresholds that feed the existing Native Promotion Gate
(A357) and the regression benchmark framework.

Budget categories:
    - cold_startup_ms:      import + first-call wall time
    - warm_p50_ms:         warm-path p50 wall time
    - warm_p95_ms:         warm-path p95 wall time
    - peak_memory_kb:      peak memory per invocation
    - allocation_count:    tracemalloc allocation count per invocation
    - call_count:          cProfile total call count per invocation
    - json_serialize_ms:   JSON encode/decode time
    - db_query_count:      SQL queries per operation
    - native_crossings:    Python<->native boundary crossings per op
    - logging_overhead_ms: logging time per operation

Each budget has a ``tolerance_pct`` (default 20%) — exceeding the budget
by less than the tolerance is a WARN; exceeding by more is a FAIL.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class PerformanceBudget:
    """Performance budget for one capability."""
    capability_id: str
    cold_startup_ms: float = 100.0
    warm_p50_ms: float = 10.0
    warm_p95_ms: float = 50.0
    peak_memory_kb: float = 1024.0
    allocation_count: int = 50000
    call_count: int = 100000
    json_serialize_ms: float = 5.0
    db_query_count: int = 10
    native_crossings: int = 0
    logging_overhead_ms: float = 2.0
    tolerance_pct: float = 20.0


@dataclass(frozen=True)
class BudgetCheckResult:
    """Result of checking a measurement against a budget."""
    capability_id: str
    metric: str
    budget: float
    actual: float
    delta_pct: float
    status: str  # PASS, WARN, FAIL
    message: str


# V1 budgets for the three native-core counterpart capabilities.
# These are initial conservative budgets based on the V1 baseline.
# They will be tightened as optimizations land.
_BUDGETS: dict[str, PerformanceBudget] = {
    "native.parser.token_estimate": PerformanceBudget(
        capability_id="native.parser.token_estimate",
        cold_startup_ms=50.0,
        warm_p50_ms=2.0,
        warm_p95_ms=10.0,
        peak_memory_kb=512.0,
        allocation_count=5000,
        call_count=100,
        json_serialize_ms=1.0,
        db_query_count=0,
        native_crossings=0,
        logging_overhead_ms=0.5,
    ),
    "native.vector.similarity": PerformanceBudget(
        capability_id="native.vector.similarity",
        cold_startup_ms=10.0,
        warm_p50_ms=1.0,
        warm_p95_ms=5.0,
        peak_memory_kb=100.0,
        allocation_count=5000,
        call_count=1000,
        json_serialize_ms=0.5,
        db_query_count=0,
        native_crossings=0,
        logging_overhead_ms=0.5,
    ),
    "native.transformer.attention": PerformanceBudget(
        capability_id="native.transformer.attention",
        cold_startup_ms=10.0,
        warm_p50_ms=5.0,
        warm_p95_ms=20.0,
        peak_memory_kb=256.0,
        allocation_count=10000,
        call_count=5000,
        json_serialize_ms=0.5,
        db_query_count=0,
        native_crossings=0,
        logging_overhead_ms=0.5,
    ),
    # Python runtime path budgets (not native-core counterparts)
    "python.rag.chunking": PerformanceBudget(
        capability_id="python.rag.chunking",
        cold_startup_ms=200.0,  # tiktoken import is heavy
        warm_p50_ms=50.0,
        warm_p95_ms=200.0,
        peak_memory_kb=2048.0,
        allocation_count=100000,
        call_count=50000,
        json_serialize_ms=2.0,
        db_query_count=0,
        native_crossings=0,
        logging_overhead_ms=1.0,
    ),
    "python.store.encode_json": PerformanceBudget(
        capability_id="python.store.encode_json",
        cold_startup_ms=5.0,
        warm_p50_ms=0.5,
        warm_p95_ms=2.0,
        peak_memory_kb=1024.0,
        allocation_count=1000,
        call_count=100,
        json_serialize_ms=1.0,
        db_query_count=0,
        native_crossings=0,
        logging_overhead_ms=0.1,
    ),
    "python.reconcile.mark_pending": PerformanceBudget(
        capability_id="python.reconcile.mark_pending",
        cold_startup_ms=5.0,
        warm_p50_ms=1.0,
        warm_p95_ms=5.0,
        peak_memory_kb=512.0,
        allocation_count=5000,
        call_count=1000,
        json_serialize_ms=0.5,
        db_query_count=1,
        native_crossings=0,
        logging_overhead_ms=0.5,
    ),
}


def get_budget(capability_id: str) -> PerformanceBudget | None:
    """Get the performance budget for a capability."""
    return _BUDGETS.get(capability_id)


def list_budgets() -> list[str]:
    """List all capability IDs with budgets."""
    return sorted(_BUDGETS.keys())


def check_budget(
    capability_id: str,
    metric: str,
    actual: float,
) -> BudgetCheckResult:
    """Check a measurement against a budget.

    Returns a BudgetCheckResult with status:
        PASS: actual <= budget
        WARN: actual > budget but within tolerance
        FAIL: actual > budget + tolerance
    """
    budget = _BUDGETS.get(capability_id)
    if budget is None:
        return BudgetCheckResult(
            capability_id=capability_id,
            metric=metric,
            budget=0.0,
            actual=actual,
            delta_pct=0.0,
            status="PASS",
            message=f"no budget for {capability_id}",
        )

    budget_value = getattr(budget, metric, None)
    if budget_value is None:
        return BudgetCheckResult(
            capability_id=capability_id,
            metric=metric,
            budget=0.0,
            actual=actual,
            delta_pct=0.0,
            status="PASS",
            message=f"no budget metric {metric} for {capability_id}",
        )

    if budget_value == 0:
        delta_pct = 0.0 if actual == 0 else float("inf")
    else:
        delta_pct = ((actual - budget_value) / budget_value) * 100.0

    if actual <= budget_value:
        status = "PASS"
        message = f"{metric} {actual:.2f} <= budget {budget_value:.2f}"
    elif delta_pct <= budget.tolerance_pct:
        status = "WARN"
        message = (
            f"{metric} {actual:.2f} exceeds budget {budget_value:.2f} "
            f"by {delta_pct:.1f}% (within {budget.tolerance_pct}% tolerance)"
        )
    else:
        status = "FAIL"
        message = (
            f"{metric} {actual:.2f} exceeds budget {budget_value:.2f} "
            f"by {delta_pct:.1f}% (above {budget.tolerance_pct}% tolerance)"
        )

    return BudgetCheckResult(
        capability_id=capability_id,
        metric=metric,
        budget=float(budget_value),
        actual=actual,
        delta_pct=round(delta_pct, 2),
        status=status,
        message=message,
    )


def check_all_budgets(
    capability_id: str,
    measurements: dict[str, float],
) -> list[BudgetCheckResult]:
    """Check all measurements for a capability against its budget."""
    return [
        check_budget(capability_id, metric, value)
        for metric, value in measurements.items()
    ]


__all__ = [
    "PerformanceBudget",
    "BudgetCheckResult",
    "get_budget",
    "list_budgets",
    "check_budget",
    "check_all_budgets",
]
