"""Benchmark — Python/native comparable benchmark framework.

Each benchmark uses the same input/correctness contract and covers:
    - size: small / medium / large
    - warmth: cold (first call) / warm (after warmup)
    - concurrency: bounded (clamped to the five-core thread budget, §10.30)

For native comparisons, the benchmark measures end-to-end cost:
    Python call → data conversion → pybind11/C ABI → C++ →
    result conversion → Python return
NOT compute-only (A358).
"""
from __future__ import annotations

import concurrent.futures
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Sequence

from .profiler import ProfileResult, _percentile, profile_callable
from .thread_budget import bounded_workers


class SizeClass(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class WarmthClass(str, Enum):
    COLD = "cold"
    WARM = "warm"


@dataclass(frozen=True)
class BenchmarkConfig:
    """Configuration for one benchmark run."""
    capability_id: str
    size: SizeClass
    warmth: WarmthClass
    concurrency: int = 1
    warmup_count: int = 3
    sample_count: int = 20


@dataclass(frozen=True)
class BenchmarkResult:
    """Result of one benchmark configuration."""
    config: BenchmarkConfig
    wall_p50: float
    wall_p95: float
    wall_p99: float
    cpu_p50: float
    peak_memory_p50: float
    allocation_p50: float
    call_count_median: int
    correctness_passed: bool
    hottest_callables: tuple[tuple[str, int, float], ...] = ()


class BenchmarkSuite:
    """Runs a callable across the full benchmark matrix.

    The ``python_fn`` produces a result; ``correctness_fn`` validates it
    against the same contract for every configuration.  When a
    ``native_fn`` is provided, the suite also measures the native path
    end-to-end (including data conversion, copy, and return conversion).
    """

    def __init__(
        self,
        capability_id: str,
        python_fn: Callable[..., Any],
        correctness_fn: Callable[[Any], bool],
        native_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.capability_id = capability_id
        self.python_fn = python_fn
        self.correctness_fn = correctness_fn
        self.native_fn = native_fn

    def run_single(
        self,
        config: BenchmarkConfig,
        input_factory: Callable[[SizeClass], Any],
    ) -> BenchmarkResult:
        """Run one benchmark configuration."""
        args = input_factory(config.size)

        # Warmup
        if config.warmth == WarmthClass.WARM:
            for _ in range(config.warmup_count):
                self.python_fn(args)

        # Concurrency execution
        walls: list[float] = []
        cpus: list[float] = []
        mems: list[int] = []
        allocs: list[int] = []
        calls: list[int] = []
        correctness = True
        last_hot: tuple[tuple[str, int, float], ...] = ()

        def _one() -> ProfileResult:
            return profile_callable(self.python_fn, args)

        if config.concurrency <= 1:
            for _ in range(config.sample_count):
                result = _one()
                walls.append(result.wall_seconds)
                cpus.append(result.cpu_seconds)
                mems.append(result.peak_memory_bytes)
                allocs.append(result.allocation_count)
                calls.append(result.call_count)
                last_hot = result.hottest_callables
                if not self.correctness_fn(self.python_fn(args)):
                    correctness = False
        else:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=bounded_workers(config.concurrency),
            ) as pool:
                futures = [
                    pool.submit(_one)
                    for _ in range(config.sample_count)
                ]
                for fut in concurrent.futures.as_completed(futures):
                    result = fut.result()
                    walls.append(result.wall_seconds)
                    cpus.append(result.cpu_seconds)
                    mems.append(result.peak_memory_bytes)
                    allocs.append(result.allocation_count)
                    calls.append(result.call_count)
                    last_hot = result.hottest_callables
                    if not self.correctness_fn(self.python_fn(args)):
                        correctness = False

        return BenchmarkResult(
            config=config,
            wall_p50=_percentile(walls, 50),
            wall_p95=_percentile(walls, 95),
            wall_p99=_percentile(walls, 99),
            cpu_p50=_percentile(cpus, 50),
            peak_memory_p50=_percentile(mems, 50),
            allocation_p50=_percentile(allocs, 50),
            call_count_median=int(statistics.median(calls)) if calls else 0,
            correctness_passed=correctness,
            hottest_callables=last_hot,
        )

    def run_matrix(
        self,
        input_factory: Callable[[SizeClass], Any],
        *,
        sizes: Sequence[SizeClass] = (SizeClass.SMALL, SizeClass.MEDIUM, SizeClass.LARGE),
        warmths: Sequence[WarmthClass] = (WarmthClass.COLD, WarmthClass.WARM),
        concurrencies: Sequence[int] = (1, 4),
        sample_count: int = 20,
        warmup_count: int = 3,
    ) -> list[BenchmarkResult]:
        """Run the full benchmark matrix."""
        results: list[BenchmarkResult] = []
        for size in sizes:
            for warmth in warmths:
                for conc in concurrencies:
                    config = BenchmarkConfig(
                        capability_id=self.capability_id,
                        size=size,
                        warmth=warmth,
                        concurrency=conc,
                        warmup_count=warmup_count,
                        sample_count=sample_count,
                    )
                    results.append(self.run_single(config, input_factory))
        return results

    def run_native_comparison(
        self,
        config: BenchmarkConfig,
        input_factory: Callable[[SizeClass], Any],
    ) -> dict[str, BenchmarkResult]:
        """Run Python vs native end-to-end comparison.

        Returns ``{"python": ..., "native": ...}``.  If native is
        unavailable, the native result is None and the Python fallback
        is preserved (A219).
        """
        python_result = self.run_single(config, input_factory)
        if self.native_fn is None:
            return {"python": python_result, "native": None}
        # Measure native end-to-end (including conversion + copy + return)
        native_suite = BenchmarkSuite(
            capability_id=f"{self.capability_id}.native",
            python_fn=self.native_fn,
            correctness_fn=self.correctness_fn,
        )
        native_result = native_suite.run_single(config, input_factory)
        return {"python": python_result, "native": native_result}


__all__ = [
    "SizeClass",
    "WarmthClass",
    "BenchmarkConfig",
    "BenchmarkResult",
    "BenchmarkSuite",
]
