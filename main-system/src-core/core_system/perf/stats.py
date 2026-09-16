"""Shared percentile math for core_system performance tooling.

Single canonical implementation — previously duplicated in
``native/parallel_benchmark.py``, ``rag/benchmark.py`` and
``perf/e2e/benchmark.py`` with three different signatures.  ``q`` is a
fraction in [0, 1]; callers with 0-100 conventions divide by 100.
"""
from __future__ import annotations

from typing import Sequence


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile; ``q`` is a fraction in [0, 1]."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = int(len(ordered) * min(max(q, 0.0), 1.0))
    return ordered[min(index, len(ordered) - 1)]


def percentiles_p50_p95_p99(
    values: Sequence[float],
) -> tuple[float, float, float]:
    ordered = sorted(values)
    return (
        percentile(ordered, 0.50),
        percentile(ordered, 0.95),
        percentile(ordered, 0.99),
    )


__all__ = ["percentile", "percentiles_p50_p95_p99"]
