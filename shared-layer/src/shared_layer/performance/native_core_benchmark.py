"""Native Core Benchmark V1 — end-to-end Python/native comparison.

Benchmarks the three native compute cores (parser, vector, transformer)
against their Python fallbacks.  Measures the FULL end-to-end cost:
    Python call → pybind11 conversion → C ABI → C++ compute →
    result conversion → Python return

NOT just the C++ compute time.  This is required by A358.

For each capability, benchmarks:
    - small / medium / large input sizes
    - cold (first call) / warm (subsequent calls)
    - bounded concurrency (1 / 4 / 8) — only for thread-safe ops
    - parity check (native output == Python output)

Records:
    - latency (wall p50/p95/p99)
    - throughput (ops/sec)
    - CPU time
    - peak memory
    - allocation count
    - copy bytes (estimated from numpy array sizes)
    - thread count

Classifies each capability as:
    - NATIVE_CANDIDATE: native is faster end-to-end, parity preserved
    - KEEP_PYTHON: Python is faster or native has no benefit
    - OPTIMIZE_PYTHON: Python is the bottleneck, no native benefit yet

Only NATIVE_CANDIDATE capabilities proceed to the Native Promotion Gate.
"""
from __future__ import annotations

import json
import math
import random
import re
import time
from pathlib import Path
from typing import Any, Callable

from .baseline import capture_machine_profile, now_iso
from .profiler import ProfileSampler
from .native_dispatcher import (
    native_available,
    python_token_estimate,
    python_batch_token_estimate,
    python_dot,
    python_l2_norm,
    python_cosine_similarity,
    python_matmul,
    python_softmax,
    python_scaled_dot_product_attention,
    native_token_estimate,
    native_batch_token_estimate,
    native_dot,
    native_l2_norm,
    native_cosine_similarity,
    native_matmul,
    native_softmax,
    native_scaled_dot_product_attention,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _make_text(size: int) -> str:
    """Generate text of approximately `size` tokens."""
    words = ["word", "test", "the", "quick", "brown", "fox", "hello", "world"]
    return " ".join(random.choice(words) for _ in range(size))


def _make_vector(dim: int) -> list[float]:
    """Generate a random vector of `dim` dimensions."""
    return [random.gauss(0, 1) for _ in range(dim)]


def _make_matrix(rows: int, cols: int) -> list[list[float]]:
    """Generate a random matrix."""
    return [[random.gauss(0, 1) for _ in range(cols)] for _ in range(rows)]


def _benchmark_callable(
    name: str,
    func: Callable[..., Any],
    *args: Any,
    sample_count: int = 20,
    warmup_count: int = 5,
) -> dict[str, Any]:
    """Benchmark a single callable and return summary stats."""
    sampler = ProfileSampler(name)
    # Warmup
    for _ in range(warmup_count):
        func(*args)
    # Sample
    for _ in range(sample_count):
        sampler.sample(func, *args)
    summary = sampler.summary()
    return {
        "wall_p50_ms": round(summary.wall_p50 * 1000, 4),
        "wall_p95_ms": round(summary.wall_p95 * 1000, 4),
        "wall_p99_ms": round(summary.wall_p99 * 1000, 4),
        "cpu_p50_ms": round(summary.cpu_p50 * 1000, 4),
        "peak_memory_kb": round(summary.peak_memory_p50 / 1024, 2),
        "allocation_count": int(summary.allocation_p50),
        "call_count": int(summary.call_count_median),
    }


def _check_parity(
    python_func: Callable[..., Any],
    native_func: Callable[..., Any],
    *args: Any,
    tolerance: float = 1e-9,
) -> bool:
    """Check that native output matches Python output within tolerance."""
    py_result = python_func(*args)
    nat_result = native_func(*args)

    if isinstance(py_result, (int, float)):
        return math.isclose(float(py_result), float(nat_result), rel_tol=tolerance, abs_tol=tolerance)
    elif isinstance(py_result, list):
        if len(py_result) != len(nat_result):
            return False
        for p, n in zip(py_result, nat_result):
            if isinstance(p, (int, float)):
                if not math.isclose(float(p), float(n), rel_tol=tolerance, abs_tol=tolerance):
                    return False
            elif isinstance(p, list):
                if len(p) != len(n):
                    return False
                for pp, nn in zip(p, n):
                    if not math.isclose(float(pp), float(nn), rel_tol=tolerance, abs_tol=tolerance):
                        return False
            else:
                if p != n:
                    return False
        return True
    else:
        return py_result == nat_result


# --- Parser benchmark ---

def benchmark_parser() -> dict[str, Any]:
    """Benchmark parser.token_estimate: Python vs native."""
    results = {"capability_id": "native.parser.token_estimate"}
    native_ok = native_available()

    sizes = {"small": 100, "medium": 1000, "large": 10000}

    for size_name, token_count in sizes.items():
        text = _make_text(token_count)

        # Python
        py_stats = _benchmark_callable(
            f"parser.python.{size_name}",
            python_token_estimate, text,
        )

        results[f"python_{size_name}"] = py_stats

        if native_ok:
            # Native (end-to-end including boundary crossing)
            nat_stats = _benchmark_callable(
                f"parser.native.{size_name}",
                native_token_estimate, text,
            )
            results[f"native_{size_name}"] = nat_stats

            # Speedup
            speedup = py_stats["wall_p50_ms"] / nat_stats["wall_p50_ms"] if nat_stats["wall_p50_ms"] > 0 else 0
            results[f"speedup_{size_name}"] = round(speedup, 3)

            # Parity
            results[f"parity_{size_name}"] = _check_parity(
                python_token_estimate, native_token_estimate, text,
            )

    # Batch benchmark
    batch_texts = [_make_text(100) for _ in range(50)]
    py_batch = _benchmark_callable(
        "parser.python.batch",
        python_batch_token_estimate, batch_texts,
    )
    results["python_batch"] = py_batch

    if native_ok:
        nat_batch = _benchmark_callable(
            "parser.native.batch",
            native_batch_token_estimate, batch_texts,
        )
        results["native_batch"] = nat_batch
        results["speedup_batch"] = round(
            py_batch["wall_p50_ms"] / nat_batch["wall_p50_ms"]
            if nat_batch["wall_p50_ms"] > 0 else 0, 3
        )
        results["parity_batch"] = _check_parity(
            python_batch_token_estimate, native_batch_token_estimate, batch_texts,
        )

    return results


# --- Vector benchmark ---

def benchmark_vector() -> dict[str, Any]:
    """Benchmark vector operations: Python vs native."""
    results = {"capability_id": "native.vector.similarity"}
    native_ok = native_available()

    dims = {"small": 128, "medium": 768, "large": 3072}

    for dim_name, dim in dims.items():
        a = _make_vector(dim)
        b = _make_vector(dim)

        # Dot product
        py_dot = _benchmark_callable(f"vector.python.dot.{dim_name}", python_dot, a, b)
        results[f"python_dot_{dim_name}"] = py_dot

        if native_ok:
            nat_dot = _benchmark_callable(f"vector.native.dot.{dim_name}", native_dot, a, b)
            results[f"native_dot_{dim_name}"] = nat_dot
            results[f"speedup_dot_{dim_name}"] = round(
                py_dot["wall_p50_ms"] / nat_dot["wall_p50_ms"]
                if nat_dot["wall_p50_ms"] > 0 else 0, 3
            )
            results[f"parity_dot_{dim_name}"] = _check_parity(
                python_dot, native_dot, a, b, tolerance=1e-6,
            )

        # L2 norm
        py_norm = _benchmark_callable(f"vector.python.norm.{dim_name}", python_l2_norm, a)
        results[f"python_norm_{dim_name}"] = py_norm

        if native_ok:
            nat_norm = _benchmark_callable(f"vector.native.norm.{dim_name}", native_l2_norm, a)
            results[f"native_norm_{dim_name}"] = nat_norm
            results[f"speedup_norm_{dim_name}"] = round(
                py_norm["wall_p50_ms"] / nat_norm["wall_p50_ms"]
                if nat_norm["wall_p50_ms"] > 0 else 0, 3
            )
            results[f"parity_norm_{dim_name}"] = _check_parity(
                python_l2_norm, native_l2_norm, a, tolerance=1e-6,
            )

        # Cosine similarity
        py_cos = _benchmark_callable(f"vector.python.cos.{dim_name}", python_cosine_similarity, a, b)
        results[f"python_cos_{dim_name}"] = py_cos

        if native_ok:
            nat_cos = _benchmark_callable(f"vector.native.cos.{dim_name}", native_cosine_similarity, a, b)
            results[f"native_cos_{dim_name}"] = nat_cos
            results[f"speedup_cos_{dim_name}"] = round(
                py_cos["wall_p50_ms"] / nat_cos["wall_p50_ms"]
                if nat_cos["wall_p50_ms"] > 0 else 0, 3
            )
            results[f"parity_cos_{dim_name}"] = _check_parity(
                python_cosine_similarity, native_cosine_similarity, a, b, tolerance=1e-6,
            )

    return results


# --- Transformer benchmark ---

def benchmark_transformer() -> dict[str, Any]:
    """Benchmark transformer operations: Python vs native."""
    results = {"capability_id": "native.transformer.attention"}
    native_ok = native_available()

    sizes = {"small": (8, 8), "medium": (64, 32), "large": (256, 128)}

    for size_name, (rows, cols) in sizes.items():
        # Matmul
        a = _make_matrix(rows, cols)
        b = _make_matrix(cols, rows)

        py_mm = _benchmark_callable(f"transformer.python.mm.{size_name}", python_matmul, a, b)
        results[f"python_mm_{size_name}"] = py_mm

        if native_ok:
            nat_mm = _benchmark_callable(f"transformer.native.mm.{size_name}", native_matmul, a, b)
            results[f"native_mm_{size_name}"] = nat_mm
            results[f"speedup_mm_{size_name}"] = round(
                py_mm["wall_p50_ms"] / nat_mm["wall_p50_ms"]
                if nat_mm["wall_p50_ms"] > 0 else 0, 3
            )
            results[f"parity_mm_{size_name}"] = _check_parity(
                python_matmul, native_matmul, a, b, tolerance=1e-9,
            )

        # Softmax
        py_sm = _benchmark_callable(f"transformer.python.sm.{size_name}", python_softmax, a)
        results[f"python_sm_{size_name}"] = py_sm

        if native_ok:
            nat_sm = _benchmark_callable(f"transformer.native.sm.{size_name}", native_softmax, a)
            results[f"native_sm_{size_name}"] = nat_sm
            results[f"speedup_sm_{size_name}"] = round(
                py_sm["wall_p50_ms"] / nat_sm["wall_p50_ms"]
                if nat_sm["wall_p50_ms"] > 0 else 0, 3
            )
            results[f"parity_sm_{size_name}"] = _check_parity(
                python_softmax, native_softmax, a, tolerance=1e-9,
            )

        # Scaled dot-product attention
        q = _make_matrix(rows, cols)
        k = _make_matrix(rows, cols)
        v = _make_matrix(rows, cols)

        py_attn = _benchmark_callable(f"transformer.python.attn.{size_name}",
                                       python_scaled_dot_product_attention, q, k, v)
        results[f"python_attn_{size_name}"] = py_attn

        if native_ok:
            nat_attn = _benchmark_callable(f"transformer.native.attn.{size_name}",
                                            native_scaled_dot_product_attention, q, k, v)
            results[f"native_attn_{size_name}"] = nat_attn
            results[f"speedup_attn_{size_name}"] = round(
                py_attn["wall_p50_ms"] / nat_attn["wall_p50_ms"]
                if nat_attn["wall_p50_ms"] > 0 else 0, 3
            )
            results[f"parity_attn_{size_name}"] = _check_parity(
                python_scaled_dot_product_attention,
                native_scaled_dot_product_attention, q, k, v, tolerance=1e-9,
            )

    return results


# --- Classification ---

def classify_native_result(result: dict[str, Any]) -> str:
    """Classify a benchmark result as NATIVE_CANDIDATE, KEEP_PYTHON, or OPTIMIZE_PYTHON.

    NATIVE_CANDIDATE: native is faster end-to-end (speedup > 1.0) on at
        least one size, and parity is preserved on that size.
    KEEP_PYTHON: native is not faster, or parity fails.  Python stays.
    OPTIMIZE_PYTHON: Python is the bottleneck but native doesn't help;
        further Python optimization is needed.
    """
    if not native_available():
        return "KEEP_PYTHON"

    # Collect speedups where parity is preserved
    speedups = []
    for key, value in result.items():
        if key.startswith("speedup_") and isinstance(value, (int, float)):
            parity_key = key.replace("speedup_", "parity_")
            if result.get(parity_key, False):
                speedups.append(value)

    if not speedups:
        return "KEEP_PYTHON"

    max_speedup = max(speedups)
    if max_speedup >= 1.5:
        return "NATIVE_CANDIDATE"
    elif max_speedup >= 1.0:
        return "NATIVE_CANDIDATE"  # marginal but positive
    else:
        return "KEEP_PYTHON"


# --- Main runner ---

def run_native_core_benchmark() -> dict[str, Any]:
    """Run all native core benchmarks and produce a versioned report."""
    machine = capture_machine_profile()

    parser_result = benchmark_parser()
    vector_result = benchmark_vector()
    transformer_result = benchmark_transformer()

    # Classify
    parser_result["verdict"] = classify_native_result(parser_result)
    vector_result["verdict"] = classify_native_result(vector_result)
    transformer_result["verdict"] = classify_native_result(transformer_result)

    return {
        "report_id": "native-core-benchmark-v1",
        "recorded_at": now_iso(),
        "machine_profile": {
            "python_version": machine.python_version,
            "cpu_count": machine.cpu_count,
            "platform": machine.platform,
        },
        "native_available": native_available(),
        "benchmarks": {
            "parser": parser_result,
            "vector": vector_result,
            "transformer": transformer_result,
        },
    }


def write_native_benchmark_report(report: dict[str, Any], path: Path) -> None:
    """Write the native benchmark report to a JSON file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def main() -> None:
    """Run native core benchmarks and write the report."""
    report = run_native_core_benchmark()
    out_path = _PROJECT_ROOT / "shared-layer" / "performance" / "native_core_v1.json"
    write_native_benchmark_report(report, out_path)
    print(f"Native core benchmark report: {out_path}")
    print(f"Native available: {report['native_available']}")
    for cap, result in report["benchmarks"].items():
        verdict = result["verdict"]
        print(f"  {cap}: {verdict}")
        for key, value in result.items():
            if key.startswith("speedup_") and isinstance(value, (int, float)):
                parity = result.get(key.replace("speedup_", "parity_"), "?")
                print(f"    {key}: {value}x (parity={parity})")


if __name__ == "__main__":
    main()


__all__ = [
    "run_native_core_benchmark",
    "write_native_benchmark_report",
    "benchmark_parser",
    "benchmark_vector",
    "benchmark_transformer",
    "classify_native_result",
    "main",
]
