"""Profiler — reproducible profile baseline collector.

Records wall time, CPU time, p50/p95/p99, call count, peak memory,
allocation count, and hottest callables for a Python execution path.

Uses only stdlib (cProfile, time, tracemalloc) + the native process-metrics
facade (P24) so the baseline is reproducible without extra dependencies
(A37/E23: stdlib-only self-host).
"""
from __future__ import annotations

import cProfile
import pstats
import statistics
import time
import tracemalloc
from dataclasses import dataclass, field
from io import StringIO
from typing import Any, Callable, Sequence

import os

from . import process_metrics as _metrics


@dataclass(frozen=True)
class ProfileResult:
    """One profile sample for a single invocation."""
    wall_seconds: float
    cpu_seconds: float
    peak_memory_bytes: int
    allocation_count: int
    call_count: int
    hottest_callables: tuple[tuple[str, int, float], ...]  # (name, ncalls, cumtime)


@dataclass(frozen=True)
class ProfileSummary:
    """Aggregated profile across many samples."""
    label: str
    sample_count: int
    wall_p50: float
    wall_p95: float
    wall_p99: float
    cpu_p50: float
    cpu_p95: float
    cpu_p99: float
    peak_memory_p50: float
    peak_memory_p95: float
    allocation_p50: float
    allocation_p95: float
    call_count_median: int
    hottest_callables: tuple[tuple[str, int, float], ...]


def _percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    idx = int(len(sorted_vals) * p / 100)
    return sorted_vals[min(idx, len(sorted_vals) - 1)]


def _capture_hottest(profiler: cProfile.Profile, top: int = 10) -> tuple[tuple[str, int, float], ...]:
    stats = pstats.Stats(profiler)
    stats.sort_stats(pstats.SortKey.CUMULATIVE)
    buffer = StringIO()
    stats.stream = buffer
    stats.print_stats(top)
    hot = []
    for line in buffer.getvalue().splitlines():
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        # Skip header lines: data lines have a numeric ncalls and a
        # numeric cumtime in position 3 (0-indexed: parts[3]).
        ncalls_str = parts[0].split("/")[0]
        if not ncalls_str.isdigit():
            continue
        try:
            cumtime = float(parts[3])
        except ValueError:
            continue
        name = parts[5].strip()
        if name:
            ncalls = int(ncalls_str)
            hot.append((name, ncalls, cumtime))
    return tuple(hot[:top])


def profile_callable(
    func: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> ProfileResult:
    """Profile a single invocation of ``func``."""
    profiler = cProfile.Profile()
    tracemalloc.start()
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    mem_start = max(0, _metrics.process_working_set_bytes(os.getpid()))

    profiler.enable()
    func(*args, **kwargs)
    profiler.disable()

    wall = time.perf_counter() - wall_start
    cpu = time.process_time() - cpu_start
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    peak_memory = max(peak, max(0, _metrics.process_working_set_bytes(os.getpid())) - mem_start)

    stats = pstats.Stats(profiler)
    total_calls = stats.total_calls
    hot = _capture_hottest(profiler)

    return ProfileResult(
        wall_seconds=wall,
        cpu_seconds=cpu,
        peak_memory_bytes=peak_memory,
        allocation_count=current,
        call_count=total_calls,
        hottest_callables=hot,
    )


def profile_block(
    func: Callable[[], Any],
) -> ProfileResult:
    """Profile a code block (no args) — same as ``profile_callable``."""
    return profile_callable(func)


class ProfileSampler:
    """Collect multiple profile samples and aggregate them."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._samples: list[ProfileResult] = []

    def sample(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> ProfileResult:
        result = profile_callable(func, *args, **kwargs)
        self._samples.append(result)
        return result

    def summary(self) -> ProfileSummary:
        if not self._samples:
            return ProfileSummary(
                label=self.label, sample_count=0,
                wall_p50=0, wall_p95=0, wall_p99=0,
                cpu_p50=0, cpu_p95=0, cpu_p99=0,
                peak_memory_p50=0, peak_memory_p95=0,
                allocation_p50=0, allocation_p95=0,
                call_count_median=0, hottest_callables=(),
            )
        walls = [s.wall_seconds for s in self._samples]
        cpus = [s.cpu_seconds for s in self._samples]
        mems = [s.peak_memory_bytes for s in self._samples]
        allocs = [s.allocation_count for s in self._samples]
        calls = [s.call_count for s in self._samples]
        # Hottest callables from the most recent sample
        hot = self._samples[-1].hottest_callables
        return ProfileSummary(
            label=self.label,
            sample_count=len(self._samples),
            wall_p50=_percentile(walls, 50),
            wall_p95=_percentile(walls, 95),
            wall_p99=_percentile(walls, 99),
            cpu_p50=_percentile(cpus, 50),
            cpu_p95=_percentile(cpus, 95),
            cpu_p99=_percentile(cpus, 99),
            peak_memory_p50=_percentile(mems, 50),
            peak_memory_p95=_percentile(mems, 95),
            allocation_p50=_percentile(allocs, 50),
            allocation_p95=_percentile(allocs, 95),
            call_count_median=int(statistics.median(calls)),
            hottest_callables=hot,
        )


__all__ = [
    "ProfileResult",
    "ProfileSummary",
    "ProfileSampler",
    "profile_callable",
    "profile_block",
]
