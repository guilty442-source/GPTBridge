"""Parser Benchmark — chunking/tokenization (parser.cpp counterpart).

Validates the existing parser.cpp capability by benchmarking the
Python parsing path (token estimation, text analysis, chunking) against
a representative workload.  No native path is added without profile
evidence (A357/A358).

The benchmark uses a pure-Python token estimator (whitespace + punctuation
splitting) that mirrors the kind of parsing compute parser.cpp would own.
It does NOT require tiktoken or the full RAG pipeline so the baseline is
reproducible in any environment (A37/E23: stdlib-only self-host).
"""
from __future__ import annotations

import re
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

_CAPABILITY_ID = "native.parser.token_estimate"

_TOKEN_RE = re.compile(r"\b\w+\b|[^\w\s]")


def python_token_estimate(text: str) -> int:
    """Pure-Python token estimator (parser.cpp counterpart).

    Estimates token count via word + punctuation splitting.  This is
    the kind of parsing compute that parser.cpp would own if profile
    evidence shows a CPU bottleneck (A221).
    """
    return len(_TOKEN_RE.findall(text))


def python_chunk_text(text: str, chunk_size: int = 512, overlap: int = 64) -> list[str]:
    """Pure-Python text chunking with overlap."""
    tokens = _TOKEN_RE.findall(text)
    if len(tokens) <= chunk_size:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        chunk_tokens = tokens[start:end]
        chunks.append(" ".join(chunk_tokens))
        if end >= len(tokens):
            break
        start = end - overlap
    return chunks


def _input_factory(size: SizeClass) -> str:
    """Generate representative text input by size class."""
    word = "benchmark "
    if size == SizeClass.SMALL:
        return word * 100  # ~100 tokens
    if size == SizeClass.MEDIUM:
        return word * 5000  # ~5000 tokens
    return word * 50000  # ~50000 tokens


def _correctness(result: Any) -> bool:
    return isinstance(result, int) and result > 0


def run_parser_benchmark(
    sample_count: int = 10,
    warmup_count: int = 2,
) -> dict[str, Any]:
    """Run the parser benchmark matrix and classify the hotspot."""
    suite = BenchmarkSuite(
        capability_id=_CAPABILITY_ID,
        python_fn=python_token_estimate,
        correctness_fn=_correctness,
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
    large_text = _input_factory(SizeClass.LARGE)
    for _ in range(sample_count):
        sampler.sample(python_token_estimate, large_text)
    summary = sampler.summary()

    classification = classify_hotspot(
        wall_seconds=summary.wall_p50,
        cpu_seconds=summary.cpu_p50,
        peak_memory_bytes=int(summary.peak_memory_p50),
        allocation_count=int(summary.allocation_p50),
        call_count=summary.call_count_median,
        hottest_callables=summary.hottest_callables,
    )

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
        "evidence": candidate.evidence,
    }


__all__ = [
    "python_token_estimate",
    "python_chunk_text",
    "run_parser_benchmark",
]
