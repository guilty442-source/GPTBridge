"""A549 hybrid-plane benchmarks — bounded evidence for the DAG/CAG path.

A549 requires benchmark evidence for the DAG/CAG/RAG hybrid architecture.
This module measures the planner, the executor and the gated cache lookup
with hard per-operation budgets; a report is only within budget when every
operation's p95 stays inside its declared bound.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from typing import Callable, Mapping

from .cag import CacheLevel, CacheRequest, CagCacheStore
from .dag import (
    REGISTERED_NODE_CATALOG,
    RagDagExecutor,
    RagDagKind,
    RagDagNodeType,
    RagDagPlanRequest,
    RagDagPlanner,
    default_budgets,
    execution_context,
)

DAG_PLAN_P95_BUDGET_MS = 5.0
DAG_EXECUTE_P95_BUDGET_MS = 50.0
CAG_LOOKUP_P95_BUDGET_MS = 2.0


@dataclass(frozen=True, slots=True)
class OperationMetrics:
    """Timing metrics for one benchmarked operation."""

    operation: str
    iterations: int
    total_ms: float
    min_ms: float
    p50_ms: float
    p95_ms: float
    max_ms: float
    ops_per_second: float
    budget_ms: float

    @property
    def within_budget(self) -> bool:
        return self.p95_ms <= self.budget_ms

    def to_record(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "iterations": self.iterations,
            "total_ms": round(self.total_ms, 3),
            "min_ms": round(self.min_ms, 3),
            "p50_ms": round(self.p50_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "max_ms": round(self.max_ms, 3),
            "ops_per_second": round(self.ops_per_second, 1),
            "budget_ms": self.budget_ms,
            "within_budget": self.within_budget,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Aggregated benchmark evidence for the hybrid plane."""

    generated_at: float
    operations: tuple[OperationMetrics, ...]

    @property
    def all_within_budget(self) -> bool:
        return all(operation.within_budget for operation in self.operations)

    def to_record(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "all_within_budget": self.all_within_budget,
            "operations": [operation.to_record() for operation in self.operations],
        }


def _measure(
    operation: str,
    iterations: int,
    call: Callable[[int], None],
    budget_ms: float,
) -> OperationMetrics:
    samples: list[float] = []
    started = time.perf_counter()
    for index in range(iterations):
        sample_start = time.perf_counter()
        call(index)
        samples.append((time.perf_counter() - sample_start) * 1000.0)
    total_ms = (time.perf_counter() - started) * 1000.0
    ordered = sorted(samples)
    p95_index = min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))
    return OperationMetrics(
        operation=operation,
        iterations=iterations,
        total_ms=total_ms,
        min_ms=ordered[0],
        p50_ms=statistics.median(ordered),
        p95_ms=ordered[p95_index],
        max_ms=ordered[-1],
        ops_per_second=(iterations / (total_ms / 1000.0)) if total_ms > 0 else 0.0,
        budget_ms=budget_ms,
    )


def _query_request() -> RagDagPlanRequest:
    return RagDagPlanRequest(
        kind=RagDagKind.QUERY,
        dag_id="dag-benchmark",
        module_ids=("ai-assistant",),
        data_categories=("internal",),
    )


def _context():
    return execution_context(
        execution_id="exec-benchmark",
        correlation_id="corr-benchmark",
        actor_id="governance/tool/ai-assistant",
        module_id="ai-assistant",
        request_id="req-benchmark",
        decision_id="dec-benchmark",
        module_ids=("ai-assistant",),
        data_categories=("internal",),
        permission_scope="rag:query",
        budgets=default_budgets(max_steps=32, max_seconds=30.0),
    )


def _handlers() -> Mapping[RagDagNodeType, Callable]:
    def make(node_type: RagDagNodeType):
        evidence_keys = REGISTERED_NODE_CATALOG[node_type].required_evidence

        def handler(node, context, upstream):
            return {
                "ok": True,
                "evidence": {key: f"{key}:{node.node_id}" for key in evidence_keys},
            }

        return handler

    return {node_type: make(node_type) for node_type in RagDagNodeType}


def _cache_request() -> CacheRequest:
    return CacheRequest(
        tenant_id="tenant-bench",
        module_ids=("ai-assistant",),
        identity_id="identity-bench",
        permission_scope="rag:query",
        data_classification="internal",
        query="benchmark query",
        model_version="qwen3-4b@rev1",
        policy_version="v3",
        source_revision="rev-bench",
        level=CacheLevel.L1,
    )


def benchmark_hybrid_plane(
    *,
    iterations: int = 200,
    planner: RagDagPlanner | None = None,
    executor: RagDagExecutor | None = None,
    store: CagCacheStore | None = None,
) -> BenchmarkReport:
    """Run the bounded planner/executor/cache benchmarks and report p95."""
    iterations = max(1, int(iterations))
    planner = planner or RagDagPlanner()
    executor = executor or RagDagExecutor(_handlers())
    store = store or CagCacheStore()

    request = _query_request()
    context = _context()
    plan = planner.plan(request, context)

    cache_request = _cache_request()
    store.put(cache_request, {"answer_text": "benchmark"})

    def plan_call(_index: int) -> None:
        planner.plan(request, context)

    def execute_call(_index: int) -> None:
        executor.execute(plan)

    def lookup_call(_index: int) -> None:
        entry, decision = store.get(cache_request)
        if entry is None or not decision.allowed:
            raise AssertionError("benchmark cache lookup must hit")

    operations = (
        _measure("dag-plan", iterations, plan_call, DAG_PLAN_P95_BUDGET_MS),
        _measure("dag-execute", iterations, execute_call, DAG_EXECUTE_P95_BUDGET_MS),
        _measure("cag-lookup", iterations, lookup_call, CAG_LOOKUP_P95_BUDGET_MS),
    )
    return BenchmarkReport(generated_at=time.time(), operations=operations)


__all__ = [
    "CAG_LOOKUP_P95_BUDGET_MS",
    "DAG_EXECUTE_P95_BUDGET_MS",
    "DAG_PLAN_P95_BUDGET_MS",
    "BenchmarkReport",
    "OperationMetrics",
    "benchmark_hybrid_plane",
]
