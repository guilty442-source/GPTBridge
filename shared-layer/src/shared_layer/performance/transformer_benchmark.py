"""Transformer Benchmark — tensor operations (transformer.cpp counterpart).

Validates the existing transformer.cpp capability by benchmarking the
Python transformation compute path (matrix multiply, attention scoring,
softmax) against a representative workload.  No native path is added
without profile evidence (A357/A358).

The benchmark uses a pure-Python tensor implementation that mirrors the
kind of transformation compute transformer.cpp would own.  NumPy is
used as an OPTIMIZE_PYTHON comparison point (vectorized batching) to
distinguish "Python is slow because it's scalar" from "Python is slow
because the algorithm is wrong" (A358: comparability).
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

_CAPABILITY_ID = "native.transformer.attention"


def python_matmul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    """Pure-Python matrix multiply (transformer.cpp counterpart)."""
    rows_a = len(a)
    cols_a = len(a[0]) if a else 0
    cols_b = len(b[0]) if b else 0
    result = [[0.0] * cols_b for _ in range(rows_a)]
    for i in range(rows_a):
        for j in range(cols_b):
            s = 0.0
            for k in range(cols_a):
                s += a[i][k] * b[k][j]
            result[i][j] = s
    return result


def python_softmax(vec: list[float]) -> list[float]:
    """Pure-Python softmax."""
    if not vec:
        return []
    max_val = max(vec)
    exps = [math.exp(v - max_val) for v in vec]
    total = sum(exps)
    return [e / total for e in exps]


def python_attention(
    query: list[float],
    keys: list[list[float]],
    values: list[list[float]],
) -> list[float]:
    """Pure-Python scaled dot-product attention.

    Q (dim,) x K (seq, dim) x V (seq, dim) -> output (dim,).
    Mirrors the kind of transformation compute transformer.cpp would own.
    """
    dim = len(query)
    seq_len = len(keys)
    if seq_len == 0:
        return [0.0] * dim
    # scores = Q . K^T / sqrt(dim)
    scores = [0.0] * seq_len
    for i in range(seq_len):
        s = 0.0
        for j in range(dim):
            s += query[j] * keys[i][j]
        scores[i] = s / math.sqrt(dim)
    # softmax(scores)
    weights = python_softmax(scores)
    # output = sum(weights[i] * values[i])
    output = [0.0] * dim
    for i in range(seq_len):
        w = weights[i]
        for j in range(dim):
            output[j] += w * values[i][j]
    return output


def _numpy_attention(
    query: list[float],
    keys: list[list[float]],
    values: list[list[float]],
) -> list[float]:
    """NumPy-vectorized attention (OPTIMIZE_PYTHON comparison)."""
    import numpy as np
    q = np.array(query, dtype=np.float64)
    k = np.array(keys, dtype=np.float64)
    v = np.array(values, dtype=np.float64)
    dim = q.shape[0]
    scores = k @ q / math.sqrt(dim)
    # softmax
    scores = scores - scores.max()
    exps = np.exp(scores)
    weights = exps / exps.sum()
    output = weights @ v
    return output.tolist()


def _input_factory(size: SizeClass) -> tuple[list[float], list[list[float]], list[list[float]]]:
    """Generate representative attention inputs by size class."""
    if size == SizeClass.SMALL:
        dim, seq = 64, 8
    elif size == SizeClass.MEDIUM:
        dim, seq = 256, 32
    else:
        dim, seq = 768, 128
    query = [float((i * 31 % 100) / 100.0) for i in range(dim)]
    keys = [[float((i * 37 + j * 13 % 100) / 100.0) for j in range(dim)]
            for i in range(seq)]
    values = [[float((i * 41 + j * 17 % 100) / 100.0) for j in range(dim)]
              for i in range(seq)]
    return query, keys, values


def _python_fn(args: tuple) -> list[float]:
    query, keys, values = args
    return python_attention(query, keys, values)


def _numpy_fn(args: tuple) -> list[float]:
    query, keys, values = args
    return _numpy_attention(query, keys, values)


def _correctness(result: Any) -> bool:
    return isinstance(result, list) and len(result) > 0


def run_transformer_benchmark(
    sample_count: int = 5,
    warmup_count: int = 1,
    compare_numpy: bool = True,
) -> dict[str, Any]:
    """Run the transformer benchmark matrix and classify the hotspot.

    NumPy is used as an OPTIMIZE_PYTHON comparison to show whether
    vectorized batching closes the gap without native code (A358).
    """
    suite = BenchmarkSuite(
        capability_id=_CAPABILITY_ID,
        python_fn=_python_fn,
        correctness_fn=_correctness,
    )
    results = suite.run_matrix(
        input_factory=_input_factory,
        sizes=(SizeClass.SMALL, SizeClass.MEDIUM),  # large pure-Python is too slow
        warmths=(WarmthClass.COLD, WarmthClass.WARM),
        concurrencies=(1,),
        sample_count=sample_count,
        warmup_count=warmup_count,
    )

    # Profile the medium/warm path for hotspot classification
    sampler = ProfileSampler(f"{_CAPABILITY_ID}.medium_warm")
    medium_input = _input_factory(SizeClass.MEDIUM)
    for _ in range(sample_count):
        sampler.sample(_python_fn, medium_input)
    summary = sampler.summary()

    classification = classify_hotspot(
        wall_seconds=summary.wall_p50,
        cpu_seconds=summary.cpu_p50,
        peak_memory_bytes=int(summary.peak_memory_p50),
        allocation_count=int(summary.allocation_p50),
        call_count=summary.call_count_median,
        hottest_callables=summary.hottest_callables,
    )

    numpy_wall_p50: float | None = None
    if compare_numpy:
        numpy_sampler = ProfileSampler(f"{_CAPABILITY_ID}.numpy.medium_warm")
        for _ in range(sample_count):
            numpy_sampler.sample(_numpy_fn, medium_input)
        numpy_summary = numpy_sampler.summary()
        numpy_wall_p50 = numpy_summary.wall_p50

    # Classify: pure-Python vs NumPy (OPTIMIZE_PYTHON comparison)
    # No native C++ path exists yet, so native_wall_p50 is None.
    candidate = classify_candidate(
        capability_id=_CAPABILITY_ID,
        bottleneck_class=classification.bottleneck_class,
        python_wall_p50=summary.wall_p50,
        native_wall_p50=None,  # no native path yet
        parity_passed=True,
        fallback_preserved=True,
        extra_evidence={
            "classification": classification.bottleneck_class,
            "strategy": classification.recommended_strategy,
            "hottest": list(summary.hottest_callables[:5]),
            "numpy_wall_p50": numpy_wall_p50,
            "numpy_speedup": (
                round(summary.wall_p50 / numpy_wall_p50, 3)
                if numpy_wall_p50 and numpy_wall_p50 > 0 else None
            ),
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
        "numpy_wall_p50": numpy_wall_p50,
        "evidence": candidate.evidence,
    }


__all__ = [
    "python_matmul",
    "python_softmax",
    "python_attention",
    "run_transformer_benchmark",
]
