"""Classifier — hotspot bottleneck classification.

Classifies a profiled execution path into one of:
    CPU-bound, memory/copy-bound, SQL-bound, I/O-bound, model-wait, concurrency.

The classification drives the optimization strategy:
    CPU-bound       → consider native (NATIVE_CANDIDATE if profile-proven)
    memory/copy-bound → batching, boundary-copy reduction, zero-copy
    SQL-bound       → SQL pushdown, query tuning, indexing
    I/O-bound       → async, batching, caching
    model-wait      → batching, concurrency, async (not native)
    concurrency     → async, batching, lock reduction (not native)

Only CPU-bound and memory/copy-bound with profile evidence become
NATIVE_CANDIDATE; the rest are KEEP_PYTHON or OPTIMIZE_PYTHON.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

BOTTLENECK_CLASSES = frozenset({
    "cpu_bound",
    "memory_copy_bound",
    "sql_bound",
    "io_bound",
    "model_wait",
    "concurrency",
    "unknown",
})


@dataclass(frozen=True)
class HotspotClassification:
    """Classification result for a profiled hotspot."""
    bottleneck_class: str
    confidence: float  # 0.0 - 1.0
    evidence: dict[str, Any]
    recommended_strategy: str


def classify_hotspot(
    *,
    wall_seconds: float,
    cpu_seconds: float,
    peak_memory_bytes: int,
    allocation_count: int,
    call_count: int,
    io_wait_seconds: float = 0.0,
    sql_calls: int = 0,
    sql_seconds: float = 0.0,
    model_calls: int = 0,
    model_seconds: float = 0.0,
    lock_wait_seconds: float = 0.0,
    hottest_callables: tuple[tuple[str, int, float], ...] = (),
) -> HotspotClassification:
    """Classify a hotspot by its resource signature.

    Heuristics (deterministic, reproducible):
        cpu_ratio = cpu / wall
        - cpu_ratio > 0.8 and wall > 0.001 → cpu_bound
        - allocation_count > 10000 or peak_memory > 10MB → memory_copy_bound
        - sql_seconds / wall > 0.5 → sql_bound
        - io_wait_seconds / wall > 0.5 → io_bound
        - model_seconds / wall > 0.5 → model_wait
        - lock_wait_seconds / wall > 0.3 → concurrency
        - else unknown
    """
    if wall_seconds <= 0:
        return HotspotClassification(
            bottleneck_class="unknown",
            confidence=0.0,
            evidence={"wall_seconds": wall_seconds},
            recommended_strategy="reprofile with longer workload",
        )

    cpu_ratio = cpu_seconds / wall_seconds
    io_ratio = io_wait_seconds / wall_seconds if io_wait_seconds else 0.0
    sql_ratio = sql_seconds / wall_seconds if sql_seconds else 0.0
    model_ratio = model_seconds / wall_seconds if model_seconds else 0.0
    lock_ratio = lock_wait_seconds / wall_seconds if lock_wait_seconds else 0.0

    evidence = {
        "wall_seconds": wall_seconds,
        "cpu_seconds": cpu_seconds,
        "cpu_ratio": round(cpu_ratio, 3),
        "peak_memory_bytes": peak_memory_bytes,
        "allocation_count": allocation_count,
        "call_count": call_count,
        "io_wait_seconds": io_wait_seconds,
        "io_ratio": round(io_ratio, 3),
        "sql_calls": sql_calls,
        "sql_seconds": sql_seconds,
        "sql_ratio": round(sql_ratio, 3),
        "model_calls": model_calls,
        "model_seconds": model_seconds,
        "model_ratio": round(model_ratio, 3),
        "lock_wait_seconds": lock_wait_seconds,
        "lock_ratio": round(lock_ratio, 3),
    }

    # Order matters: most specific first
    if model_ratio > 0.5:
        return HotspotClassification(
            bottleneck_class="model_wait",
            confidence=min(1.0, model_ratio),
            evidence=evidence,
            recommended_strategy="batching + async concurrency (not native)",
        )
    if sql_ratio > 0.5:
        return HotspotClassification(
            bottleneck_class="sql_bound",
            confidence=min(1.0, sql_ratio),
            evidence=evidence,
            recommended_strategy="SQL pushdown + query tuning + indexing",
        )
    if io_ratio > 0.5:
        return HotspotClassification(
            bottleneck_class="io_bound",
            confidence=min(1.0, io_ratio),
            evidence=evidence,
            recommended_strategy="async I/O + batching + caching",
        )
    if lock_ratio > 0.3:
        return HotspotClassification(
            bottleneck_class="concurrency",
            confidence=min(1.0, lock_ratio),
            evidence=evidence,
            recommended_strategy="lock reduction + async + batching (not native)",
        )
    if allocation_count > 10000 or peak_memory_bytes > 10 * 1024 * 1024:
        return HotspotClassification(
            bottleneck_class="memory_copy_bound",
            confidence=min(1.0, allocation_count / 100000.0
                           + peak_memory_bytes / (100 * 1024 * 1024)),
            evidence=evidence,
            recommended_strategy="batching + boundary-copy reduction + zero-copy",
        )
    if cpu_ratio > 0.8 and wall_seconds > 0.001:
        return HotspotClassification(
            bottleneck_class="cpu_bound",
            confidence=min(1.0, cpu_ratio),
            evidence=evidence,
            recommended_strategy="consider NATIVE_CANDIDATE if profile-proven bottleneck",
        )
    return HotspotClassification(
        bottleneck_class="unknown",
        confidence=0.0,
        evidence=evidence,
        recommended_strategy="reprofile with representative workload",
    )


__all__ = [
    "HotspotClassification",
    "classify_hotspot",
    "BOTTLENECK_CLASSES",
]
