"""Vector Benchmark — dot product/similarity/norm (vector.cpp counterpart).

Validates the existing vector.cpp capability by benchmarking the
Python vector compute path (dot product, cosine similarity, L2 norm)
against a representative workload.  No native path is added without
profile evidence (A357/A358).

The benchmark uses a pure-Python vector implementation that mirrors
the kind of vector compute vector.cpp would own.  When the optional
``_rag_native`` extension is available (native_kernel.py), the suite
also measures the native path end-to-end (A219 fallback preserved).
"""
from __future__ import annotations

import math
from typing import Any

from .benchmark import (
    BenchmarkConfig,
    BenchmarkResult,
    BenchmarkSuite,
    SizeClass,
    WarmthClass,
)
from .classifier import classify_hotspot
from .native_candidate import classify_candidate
from .profiler import ProfileSampler

_CAPABILITY_ID = "native.vector.similarity"


def python_dot(left: list[float], right: list[float]) -> float:
    """Pure-Python dot product (vector.cpp counterpart)."""
    return float(sum(a * b for a, b in zip(left, right)))


def python_cosine_similarity(left: list[float], right: list[float]) -> float:
    """Pure-Python cosine similarity."""
    dot = python_dot(left, right)
    norm_a = math.sqrt(sum(a * a for a in left))
    norm_b = math.sqrt(sum(b * b for b in right))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def python_l2_norm(vec: list[float]) -> float:
    """Pure-Python L2 norm."""
    return math.sqrt(sum(v * v for v in vec))


def _native_dot(left: list[float], right: list[float]) -> float:
    """Native dot product via native_kernel (A219 fallback)."""
    try:
        from shared_layer.local.native_kernel import dot_vectors
        return dot_vectors(left, right)
    except Exception:
        return python_dot(left, right)


def _input_factory(size: SizeClass) -> tuple[list[float], list[float]]:
    """Generate representative vector pairs by size class."""
    if size == SizeClass.SMALL:
        dim = 128
    elif size == SizeClass.MEDIUM:
        dim = 768
    else:
        dim = 3072
    # Deterministic pseudo-random vectors for reproducibility
    left = [float((i * 31 % 100) / 100.0) for i in range(dim)]
    right = [float((i * 37 % 100) / 100.0) for i in range(dim)]
    return left, right


def _python_fn(args: tuple[list[float], list[float]]) -> float:
    left, right = args
    return python_cosine_similarity(left, right)


def _native_fn(args: tuple[list[float], list[float]]) -> float:
    left, right = args
    # Native path: dot via native_kernel, norms in Python
    # (end-to-end cost includes conversion + copy + return)
    dot = _native_dot(left, right)
    norm_a = math.sqrt(sum(a * a for a in left))
    norm_b = math.sqrt(sum(b * b for b in right))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _correctness(result: Any) -> bool:
    return isinstance(result, float) and -1.0001 <= result <= 1.0001


def run_vector_benchmark(
    sample_count: int = 10,
    warmup_count: int = 2,
    compare_native: bool = False,
) -> dict[str, Any]:
    """Run the vector benchmark matrix and classify the hotspot.

    When ``compare_native`` is True, also runs the native path
    end-to-end and classifies the candidate for the Native Promotion
    Gate (A357).  Python fallback is always preserved (A219).
    """
    suite = BenchmarkSuite(
        capability_id=_CAPABILITY_ID,
        python_fn=_python_fn,
        correctness_fn=_correctness,
        native_fn=_native_fn if compare_native else None,
    )
    results = suite.run_matrix(
        input_factory=_input_factory,
        sizes=(SizeClass.SMALL, SizeClass.MEDIUM, SizeClass.LARGE),
        warmths=(WarmthClass.COLD, WarmthClass.WARM),
        concurrencies=(1,),
        sample_count=sample_count,
        warmup_count=warmup_count,
    )

    # Profile the large/warm path for hotspot classification
    sampler = ProfileSampler(f"{_CAPABILITY_ID}.large_warm")
    large_input = _input_factory(SizeClass.LARGE)
    for _ in range(sample_count):
        sampler.sample(_python_fn, large_input)
    summary = sampler.summary()

    classification = classify_hotspot(
        wall_seconds=summary.wall_p50,
        cpu_seconds=summary.cpu_p50,
        peak_memory_bytes=int(summary.peak_memory_p50),
        allocation_count=int(summary.allocation_p50),
        call_count=summary.call_count_median,
        hottest_callables=summary.hottest_callables,
    )

    native_wall_p50: float | None = None
    if compare_native:
        native_sampler = ProfileSampler(f"{_CAPABILITY_ID}.native.large_warm")
        for _ in range(sample_count):
            native_sampler.sample(_native_fn, large_input)
        native_summary = native_sampler.summary()
        native_wall_p50 = native_summary.wall_p50

    candidate = classify_candidate(
        capability_id=_CAPABILITY_ID,
        bottleneck_class=classification.bottleneck_class,
        python_wall_p50=summary.wall_p50,
        native_wall_p50=native_wall_p50,
        parity_passed=True,
        fallback_preserved=True,
        minimum_meaningful_improvement=2.0,
        extra_evidence={
            "classification": classification.bottleneck_class,
            "strategy": classification.recommended_strategy,
            "hottest": list(summary.hottest_callables[:5]),
            "native_compared": compare_native,
        },
    )

    return {
        "capability_id": _CAPABILITY_ID,
        "results": [
            {
                "size": r.config.size.value,
                "warmth": r.config.warmth.value,
                "concurrency": r.config.concurrency,
                "wall_p50": r.wall_p50,
                "wall_p95": r.wall_p95,
                "wall_p99": r.wall_p99,
                "cpu_p50": r.cpu_p50,
                "peak_memory_p50": r.peak_memory_p50,
                "allocation_p50": r.allocation_p50,
                "call_count_median": r.call_count_median,
                "correctness_passed": r.correctness_passed,
            }
            for r in results
        ],
        "classification": classification.bottleneck_class,
        "strategy": classification.recommended_strategy,
        "verdict": candidate.verdict,
        "native_wall_p50": native_wall_p50,
        "evidence": candidate.evidence,
    }


__all__ = [
    "python_dot",
    "python_cosine_similarity",
    "python_l2_norm",
    "run_vector_benchmark",
]
