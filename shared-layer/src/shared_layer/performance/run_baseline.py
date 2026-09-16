"""Runner — execute all V1 benchmarks and produce versioned baseline.

Runs the three native-core counterpart benchmarks (parser, vector,
transformer) and writes a versioned baseline JSON file with machine
profile, hotspot classification, and promotion verdict for each.

This is the entry point for reproducing the V1 baseline:
    python -m shared_layer.performance.run_baseline --output baseline.json
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from .baseline import (
    BASELINE_VERSION,
    BaselineRecord,
    BaselineStore,
    capture_machine_profile,
    now_iso,
)
from .parser_benchmark import run_parser_benchmark
from .transformer_benchmark import run_transformer_benchmark
from .vector_benchmark import run_vector_benchmark


def run_all_benchmarks(
    *,
    sample_count: int = 10,
    warmup_count: int = 2,
    compare_native: bool = False,
    compare_numpy: bool = True,
) -> dict[str, dict]:
    """Run all three V1 benchmarks and return their results."""
    return {
        "native.parser.token_estimate": run_parser_benchmark(
            sample_count=sample_count,
            warmup_count=warmup_count,
        ),
        "native.vector.similarity": run_vector_benchmark(
            sample_count=sample_count,
            warmup_count=warmup_count,
            compare_native=compare_native,
        ),
        "native.transformer.attention": run_transformer_benchmark(
            sample_count=max(3, sample_count // 2),
            warmup_count=max(1, warmup_count // 2),
            compare_numpy=compare_numpy,
        ),
    }


def write_versioned_baseline(
    results: dict[str, dict],
    output_path: Path,
) -> str:
    """Write a versioned baseline JSON file with all benchmark results."""
    machine = capture_machine_profile()
    baseline_id = f"performance-baseline-v{BASELINE_VERSION}"
    record = {
        "baseline_id": baseline_id,
        "baseline_version": BASELINE_VERSION,
        "recorded_at": now_iso(),
        "machine_profile": asdict(machine),
        "benchmarks": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(record, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    return baseline_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Python Hotspot & Native Acceleration Benchmark V1",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("shared-layer/performance/baseline_v1.json"),
        help="Output baseline JSON path",
    )
    parser.add_argument("--samples", type=int, default=10)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--compare-native", action="store_true",
                        help="Compare against native_kernel (A219 fallback)")
    parser.add_argument("--no-numpy", action="store_true",
                        help="Skip NumPy comparison for transformer")
    args = parser.parse_args()

    print("Running Python Hotspot & Native Acceleration Benchmark V1...")
    results = run_all_benchmarks(
        sample_count=args.samples,
        warmup_count=args.warmup,
        compare_native=args.compare_native,
        compare_numpy=not args.no_numpy,
    )

    baseline_id = write_versioned_baseline(results, args.output)
    print(f"Baseline written: {args.output} ({baseline_id})")
    for cap_id, result in results.items():
        print(
            f"  {cap_id}: {result['classification']} -> {result['verdict']}"
        )


if __name__ == "__main__":
    main()


__all__ = [
    "run_all_benchmarks",
    "write_versioned_baseline",
    "main",
]
