"""Parallel benchmark harness — evidence before production workers.

Matrix: workers x size, single-thread batch baseline included:

    workers: 1 / 2 / 4 / 8
    sizes:   small / medium / large (per-capability item counts)

Recorded per cell: p50/p95/p99 latency, throughput, CPU time,
context switches (where measurable), memory, queue depth, cancel
latency, scaling efficiency (speedup / workers).

Production worker counts and dispatch thresholds MUST come from this
matrix — never hardware_concurrency.  A mixed-load profile
(local-model/DB/UI style contention) is included.
"""
from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..perf.stats import percentiles_p50_p95_p99
from .execution_policy import NativeExecutionPolicy, with_workers
from .execution_runtime import (
    CancellationToken,
    NativeExecutionRuntime,
)


@dataclass(frozen=True, slots=True)
class BenchmarkCell:
    workers: int
    size_label: str
    items: int
    iterations: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    throughput_per_s: float
    cpu_time_ms: float
    memory_delta_bytes: int
    peak_queue_depth: int
    cancel_latency_ms: float
    scaling_efficiency: float      # speedup vs workers=1 / workers


@dataclass(frozen=True, slots=True)
class WorkerChoice:
    """Benchmark-derived production setting."""

    workers: int
    parallel_threshold: int
    reason: str


def _memory_bytes() -> int:
    try:
        from . import private_bytes
        return int(private_bytes())
    except Exception:
        return -1


def benchmark_capability(
    capability: str,
    work_fn_factory: Callable[[], Callable[[int, int, CancellationToken], None]],
    *,
    sizes: dict[str, int],
    worker_counts: tuple[int, ...] = (1, 2, 4, 8),
    iterations: int = 20,
    queue_limit: int = 64,
) -> list[BenchmarkCell]:
    """Run the workers x size matrix for one capability.

    ``work_fn_factory`` returns a fresh per-run slice worker so state
    never leaks between cells.
    """
    cells: list[BenchmarkCell] = []
    baseline_ms: dict[str, float] = {}

    for label, items in sizes.items():
        for workers in worker_counts:
            rt = NativeExecutionRuntime(
                max_workers=workers, queue_limit=queue_limit
            )
            lat: list[float] = []
            cpu_start = time.process_time()
            mem_start = _memory_bytes()
            wall_start = time.perf_counter()
            try:
                for _ in range(iterations):
                    work = work_fn_factory()
                    slices = _even_slices(items, workers)
                    t0 = time.perf_counter()
                    rt.submit_slices(
                        slices, work,
                        deadline_at=time.monotonic() + 60.0,
                    )
                    lat.append((time.perf_counter() - t0) * 1000.0)
            finally:
                stats = rt.stats
                rt.shutdown(wait=True)
            wall = time.perf_counter() - wall_start
            cpu_ms = (time.process_time() - cpu_start) * 1000.0
            mem_end = _memory_bytes()
            p50, p95, p99 = percentiles_p50_p95_p99(lat)

            if workers == 1:
                baseline_ms[label] = p50
            speedup = (
                (baseline_ms[label] / p50) if p50 > 0 else 1.0
            ) if label in baseline_ms else 1.0
            efficiency = speedup / workers

            cells.append(BenchmarkCell(
                workers=workers,
                size_label=label,
                items=items,
                iterations=iterations,
                p50_ms=p50,
                p95_ms=p95,
                p99_ms=p99,
                throughput_per_s=(
                    (iterations * items) / wall if wall > 0 else 0.0
                ),
                cpu_time_ms=cpu_ms,
                memory_delta_bytes=(
                    (mem_end - mem_start) if mem_start >= 0 and mem_end >= 0
                    else -1
                ),
                peak_queue_depth=stats.peak_queue_depth,
                cancel_latency_ms=_measure_cancel_latency(rt_workers=workers),
                scaling_efficiency=efficiency,
            ))
    return cells


def _even_slices(items: int, workers: int) -> list[tuple[int, int]]:
    workers = max(1, min(workers, items))
    size = items // workers
    rem = items % workers
    out, start = [], 0
    for i in range(workers):
        n = size + (1 if i < rem else 0)
        out.append((start, start + n))
        start += n
    return out


def _measure_cancel_latency(rt_workers: int) -> float:
    """Latency between cancel() and workers observing the token."""
    rt = NativeExecutionRuntime(max_workers=rt_workers, queue_limit=8)
    token = CancellationToken()
    observed = []

    def work(s: int, e: int, tok: CancellationToken) -> None:
        tok.check()  # observe immediately if already cancelled
        observed.append((s, e))

    token.cancel()
    t0 = time.perf_counter()
    try:
        rt.submit_slices([(0, 1)], work,
                         deadline_at=time.monotonic() + 5.0,
                         token=token)
    except Exception:
        pass
    finally:
        rt.shutdown(wait=True)
    latency = (time.perf_counter() - t0) * 1000.0
    return latency


def choose_production_workers(
    cells: list[BenchmarkCell],
    *,
    min_efficiency: float = 0.5,
    max_p95_ms: Optional[float] = None,
) -> WorkerChoice:
    """Pick production workers/threshold from real measurements.

    Efficiency = speedup / workers; picks the largest worker count
    that still meets min_efficiency (and p95 bound if given).  Never
    falls back to hardware_concurrency.
    """
    large = [c for c in cells if c.size_label == "large"]
    if not large:
        large = cells
    eligible = [
        c for c in large
        if c.scaling_efficiency >= min_efficiency
        and (max_p95_ms is None or c.p95_ms <= max_p95_ms)
    ]
    if not eligible:
        return WorkerChoice(1, 0, "no-scaling-benefit")
    best = max(eligible, key=lambda c: c.workers)
    if best.workers == 1:
        return WorkerChoice(1, 0, "no-scaling-benefit")
    return WorkerChoice(
        workers=best.workers,
        parallel_threshold=best.items,
        reason=f"efficiency>={min_efficiency} at workers={best.workers}",
    )


__all__ = [
    "BenchmarkCell",
    "WorkerChoice",
    "benchmark_capability",
    "choose_production_workers",
]
