"""E2E benchmark runner — cold / warm / contended states, p50/p95/p99.

Each request gets a fresh trace (same identity fields pattern), runs the
path end-to-end, and is analyzed for critical-path phase shares.  Results
report user-visible E2E latency plus per-phase shares and the governed
counters (boundary crossings, SQL round-trips, serialization bytes, native
copies/allocations, queue wait vs execution).
"""
from __future__ import annotations

import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..stats import percentile as _percentile
from .critical_path import CriticalPathReport, aggregate, analyze
from .paths import PathContext, PathFn
from .spans import TraceCollector


@dataclass(frozen=True)
class PathBenchmark:
    path_name: str
    state: str                       # cold | warm | contended
    iterations: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    mean_ms: float
    phase_shares: dict[str, float]   # mean critical-path share per phase
    boundary_crossings: float        # mean per request
    sql_roundtrips: float
    serialization_bytes: float
    native_copies: float
    native_allocs: float
    queue_wait_ms: float             # mean per request
    execution_ms: float
    reports: tuple[CriticalPathReport, ...] = field(repr=False, default=())


def _report_means(reports: list[CriticalPathReport]) -> dict[str, float]:
    n = max(1, len(reports))
    return {
        "boundary_crossings": sum(r.boundary_crossings for r in reports) / n,
        "sql_roundtrips": sum(r.sql_roundtrips for r in reports) / n,
        "serialization_bytes": sum(r.serialization_bytes for r in reports) / n,
        "native_copies": sum(r.native_copies for r in reports) / n,
        "native_allocs": sum(r.native_allocs for r in reports) / n,
        "queue_wait_ms": sum(r.queue_wait_ns for r in reports) / n / 1e6,
        "execution_ms": sum(r.execution_ns for r in reports) / n / 1e6,
    }


class E2EBenchmark:
    """Runs representative paths through a PathContext of real services."""

    def __init__(self, services: PathContext,
                 *, diagnostic: bool = False) -> None:
        self.services = services
        self.diagnostic = diagnostic

    def _run_once(self, path_name: str, fn: PathFn) -> CriticalPathReport:
        collector = TraceCollector(diagnostic=self.diagnostic)
        ctx = collector.begin(path_name)
        try:
            fn(ctx, self.services)
        finally:
            ctx.close()
        return analyze(ctx.trace)

    def run(
        self,
        path_name: str,
        fn: PathFn,
        *,
        state: str = "warm",
        iterations: int = 20,
        concurrency: int = 4,
        warmup: int = 3,
    ) -> PathBenchmark:
        if state == "cold":
            # Single fresh execution — no warmup; captures first-call cost.
            reports = [self._run_once(path_name, fn)]
        elif state == "contended":
            for _ in range(warmup):
                self._run_once(path_name, fn)
            with ThreadPoolExecutor(
                max_workers=concurrency, thread_name_prefix="e2e-bench"
            ) as pool:
                reports = list(
                    pool.map(
                        lambda _: self._run_once(path_name, fn),
                        range(iterations),
                    )
                )
        else:  # warm
            for _ in range(warmup):
                self._run_once(path_name, fn)
            reports = [self._run_once(path_name, fn) for _ in range(iterations)]

        durations_ms = [r.total_ns / 1e6 for r in reports]
        means = _report_means(reports)
        return PathBenchmark(
            path_name=path_name,
            state=state,
            iterations=len(reports),
            p50_ms=_percentile(durations_ms, 0.50),
            p95_ms=_percentile(durations_ms, 0.95),
            p99_ms=_percentile(durations_ms, 0.99),
            mean_ms=statistics.fmean(durations_ms) if durations_ms else 0.0,
            phase_shares=aggregate(reports),
            boundary_crossings=means["boundary_crossings"],
            sql_roundtrips=means["sql_roundtrips"],
            serialization_bytes=means["serialization_bytes"],
            native_copies=means["native_copies"],
            native_allocs=means["native_allocs"],
            queue_wait_ms=means["queue_wait_ms"],
            execution_ms=means["execution_ms"],
            reports=tuple(reports),
        )


__all__ = ["PathBenchmark", "E2EBenchmark"]
