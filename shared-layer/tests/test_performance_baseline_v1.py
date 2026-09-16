"""Tests for Python Hotspot & Native Acceleration Benchmark V1.

Tests the performance/ package: profiler, baseline, classifier,
benchmark, native_candidate, regression, and the three native-core
counterpart benchmarks (parser, vector, transformer).

These tests use small sample counts so they run fast in CI.  They verify
the infrastructure contracts, not absolute performance numbers.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

from shared_layer.performance import (
    BenchmarkConfig,
    BenchmarkResult,
    BenchmarkSuite,
    BaselineRecord,
    BaselineStore,
    HotspotClassification,
    MachineProfile,
    NativeCandidate,
    ProfileResult,
    ProfileSampler,
    PromotionVerdict,
    RegressionEvidence,
    SizeClass,
    WarmthClass,
    BOTTLENECK_CLASSES,
    KEEP_PYTHON,
    OPTIMIZE_PYTHON,
    EXPERIMENTAL_NATIVE,
    NATIVE_CANDIDATE,
    capture_machine_profile,
    classify_candidate,
    classify_hotspot,
    compare_baselines,
    detect_regression,
    profile_block,
    profile_callable,
    to_promotion_verdict,
)
from shared_layer.performance.parser_benchmark import (
    python_chunk_text,
    python_token_estimate,
    run_parser_benchmark,
)
from shared_layer.performance.transformer_benchmark import (
    python_attention,
    python_matmul,
    python_softmax,
    run_transformer_benchmark,
)
from shared_layer.performance.vector_benchmark import (
    python_cosine_similarity,
    python_dot,
    python_l2_norm,
    run_vector_benchmark,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BASELINE_PATH = _PROJECT_ROOT / "shared-layer" / "performance" / "baseline_v1.json"


class TestProfiler:
    def test_profile_callable_returns_result(self):
        result = profile_callable(sum, [1, 2, 3])
        assert isinstance(result, ProfileResult)
        assert result.wall_seconds > 0
        assert result.cpu_seconds >= 0
        assert result.peak_memory_bytes >= 0
        assert result.allocation_count >= 0
        assert result.call_count >= 1

    def test_profile_block_works(self):
        result = profile_block(lambda: sum(range(1000)))
        assert isinstance(result, ProfileResult)
        assert result.wall_seconds > 0

    def test_profile_sampler_aggregates(self):
        sampler = ProfileSampler("test")
        for _ in range(5):
            sampler.sample(sum, range(100))
        summary = sampler.summary()
        assert summary.label == "test"
        assert summary.sample_count == 5
        assert summary.wall_p50 > 0
        assert summary.wall_p95 >= summary.wall_p50


class TestClassifier:
    def test_cpu_bound_classification(self):
        result = classify_hotspot(
            wall_seconds=1.0, cpu_seconds=0.95,
            peak_memory_bytes=1000, allocation_count=100, call_count=1000,
        )
        assert result.bottleneck_class == "cpu_bound"
        assert result.confidence > 0.8

    def test_memory_copy_bound_classification(self):
        result = classify_hotspot(
            wall_seconds=1.0, cpu_seconds=0.3,
            peak_memory_bytes=50 * 1024 * 1024, allocation_count=50000,
            call_count=100,
        )
        assert result.bottleneck_class == "memory_copy_bound"

    def test_sql_bound_classification(self):
        result = classify_hotspot(
            wall_seconds=1.0, cpu_seconds=0.1,
            peak_memory_bytes=1000, allocation_count=100, call_count=10,
            sql_calls=5, sql_seconds=0.8,
        )
        assert result.bottleneck_class == "sql_bound"

    def test_io_bound_classification(self):
        result = classify_hotspot(
            wall_seconds=1.0, cpu_seconds=0.1,
            peak_memory_bytes=1000, allocation_count=100, call_count=10,
            io_wait_seconds=0.8,
        )
        assert result.bottleneck_class == "io_bound"

    def test_model_wait_classification(self):
        result = classify_hotspot(
            wall_seconds=1.0, cpu_seconds=0.1,
            peak_memory_bytes=1000, allocation_count=100, call_count=10,
            model_calls=3, model_seconds=0.8,
        )
        assert result.bottleneck_class == "model_wait"

    def test_concurrency_classification(self):
        result = classify_hotspot(
            wall_seconds=1.0, cpu_seconds=0.2,
            peak_memory_bytes=1000, allocation_count=100, call_count=10,
            lock_wait_seconds=0.5,
        )
        assert result.bottleneck_class == "concurrency"

    def test_unknown_classification(self):
        result = classify_hotspot(
            wall_seconds=0.0001, cpu_seconds=0.00005,
            peak_memory_bytes=100, allocation_count=10, call_count=1,
        )
        assert result.bottleneck_class == "unknown"

    def test_zero_wall_returns_unknown(self):
        result = classify_hotspot(
            wall_seconds=0, cpu_seconds=0,
            peak_memory_bytes=0, allocation_count=0, call_count=0,
        )
        assert result.bottleneck_class == "unknown"

    def test_bottleneck_classes_complete(self):
        assert BOTTLENECK_CLASSES == frozenset({
            "cpu_bound", "memory_copy_bound", "sql_bound",
            "io_bound", "model_wait", "concurrency", "unknown",
        })


class TestNativeCandidate:
    def test_keep_python_for_non_cpu_bottleneck(self):
        candidate = classify_candidate(
            capability_id="test.io", bottleneck_class="io_bound",
            python_wall_p50=0.1, native_wall_p50=None,
        )
        assert candidate.verdict == OPTIMIZE_PYTHON

    def test_keep_python_for_unknown_bottleneck(self):
        candidate = classify_candidate(
            capability_id="test.unknown", bottleneck_class="unknown",
            python_wall_p50=0.1, native_wall_p50=None,
        )
        assert candidate.verdict == KEEP_PYTHON

    def test_optimize_python_for_cpu_without_native(self):
        candidate = classify_candidate(
            capability_id="test.cpu", bottleneck_class="cpu_bound",
            python_wall_p50=0.1, native_wall_p50=None,
        )
        assert candidate.verdict == OPTIMIZE_PYTHON

    def test_experimental_native_when_parity_fails(self):
        candidate = classify_candidate(
            capability_id="test.cpu", bottleneck_class="cpu_bound",
            python_wall_p50=0.1, native_wall_p50=0.02,
            parity_passed=False,
        )
        assert candidate.verdict == EXPERIMENTAL_NATIVE

    def test_experimental_native_when_fallback_missing(self):
        candidate = classify_candidate(
            capability_id="test.cpu", bottleneck_class="cpu_bound",
            python_wall_p50=0.1, native_wall_p50=0.02,
            fallback_preserved=False,
        )
        assert candidate.verdict == EXPERIMENTAL_NATIVE

    def test_experimental_native_when_speedup_below_threshold(self):
        candidate = classify_candidate(
            capability_id="test.cpu", bottleneck_class="cpu_bound",
            python_wall_p50=0.1, native_wall_p50=0.08,
            minimum_meaningful_improvement=1.5,
        )
        assert candidate.verdict == EXPERIMENTAL_NATIVE

    def test_native_candidate_when_all_pass(self):
        candidate = classify_candidate(
            capability_id="test.cpu", bottleneck_class="cpu_bound",
            python_wall_p50=0.1, native_wall_p50=0.02,
            parity_passed=True, fallback_preserved=True,
            minimum_meaningful_improvement=1.5,
        )
        assert candidate.verdict == NATIVE_CANDIDATE
        assert candidate.speedup == 5.0

    def test_native_candidate_with_latency_budget_pass(self):
        candidate = classify_candidate(
            capability_id="test.cpu", bottleneck_class="cpu_bound",
            python_wall_p50=0.1, native_wall_p50=0.02,
            budget_latency_ms=50.0,
        )
        assert candidate.verdict == NATIVE_CANDIDATE

    def test_experimental_native_when_latency_budget_exceeded(self):
        candidate = classify_candidate(
            capability_id="test.cpu", bottleneck_class="cpu_bound",
            python_wall_p50=0.1, native_wall_p50=0.06,
            budget_latency_ms=50.0,
        )
        assert candidate.verdict == EXPERIMENTAL_NATIVE

    def test_experimental_native_when_memory_budget_exceeded(self):
        candidate = classify_candidate(
            capability_id="test.mem", bottleneck_class="memory_copy_bound",
            python_wall_p50=0.1, native_wall_p50=0.02,
            budget_memory_bytes=1000, native_peak_memory_bytes=2000,
        )
        assert candidate.verdict == EXPERIMENTAL_NATIVE

    def test_promotion_verdict_pass_for_native_candidate(self):
        candidate = classify_candidate(
            capability_id="test.cpu", bottleneck_class="cpu_bound",
            python_wall_p50=0.1, native_wall_p50=0.02,
        )
        verdict = to_promotion_verdict(candidate)
        assert verdict.verdict == "PASS"
        assert "speedup" in verdict.reason

    def test_promotion_verdict_warn_for_experimental(self):
        candidate = classify_candidate(
            capability_id="test.cpu", bottleneck_class="cpu_bound",
            python_wall_p50=0.1, native_wall_p50=0.02,
            parity_passed=False,
        )
        verdict = to_promotion_verdict(candidate)
        assert verdict.verdict == "WARN"

    def test_promotion_verdict_pass_for_keep_python(self):
        candidate = classify_candidate(
            capability_id="test.io", bottleneck_class="io_bound",
            python_wall_p50=0.1, native_wall_p50=None,
        )
        verdict = to_promotion_verdict(candidate)
        assert verdict.verdict == "PASS"
        assert "keep Python" in verdict.reason


class TestBenchmarkSuite:
    def test_run_single_small_cold(self):
        suite = BenchmarkSuite(
            capability_id="test",
            python_fn=lambda x: sum(x),
            correctness_fn=lambda r: isinstance(r, int),
        )
        config = BenchmarkConfig(
            capability_id="test", size=SizeClass.SMALL,
            warmth=WarmthClass.COLD, concurrency=1,
            warmup_count=1, sample_count=3,
        )
        result = suite.run_single(config, lambda s: list(range(100)))
        assert isinstance(result, BenchmarkResult)
        assert result.wall_p50 > 0
        assert result.correctness_passed

    def test_run_matrix_covers_all_configs(self):
        suite = BenchmarkSuite(
            capability_id="test",
            python_fn=lambda x: len(x),
            correctness_fn=lambda r: isinstance(r, int),
        )
        results = suite.run_matrix(
            input_factory=lambda s: [1] * 10,
            sizes=(SizeClass.SMALL, SizeClass.MEDIUM),
            warmths=(WarmthClass.COLD, WarmthClass.WARM),
            concurrencies=(1,),
            sample_count=3, warmup_count=1,
        )
        assert len(results) == 4

    def test_native_comparison_without_native_returns_none(self):
        suite = BenchmarkSuite(
            capability_id="test",
            python_fn=lambda x: sum(x),
            correctness_fn=lambda r: isinstance(r, int),
            native_fn=None,
        )
        config = BenchmarkConfig(
            capability_id="test", size=SizeClass.SMALL,
            warmth=WarmthClass.COLD, concurrency=1,
            warmup_count=1, sample_count=3,
        )
        comparison = suite.run_native_comparison(config, lambda s: list(range(100)))
        assert comparison["python"] is not None
        assert comparison["native"] is None


class TestBaselineStore:
    def test_machine_profile_captured(self):
        mp = capture_machine_profile()
        assert isinstance(mp, MachineProfile)
        assert mp.python_version
        assert mp.cpu_count > 0

    def test_baseline_store_record_and_latest(self, tmp_path):
        store = BaselineStore(tmp_path / "baseline.json")
        record = BaselineRecord(
            baseline_id=store.next_id("test.cap"),
            baseline_version="1.0", capability_id="test.cap",
            recorded_at="2026-01-01T00:00:00Z",
            machine_profile={"python": "3.11"},
            python_metrics={"wall_p50": 0.1},
            native_metrics=None, hotspot_class="cpu_bound",
            verdict="OPTIMIZE_PYTHON",
        )
        bid = store.record(record)
        assert bid == "test.cap-v0001"
        latest = store.latest("test.cap")
        assert latest is not None
        assert latest.baseline_id == "test.cap-v0001"
        assert latest.python_metrics["wall_p50"] == 0.1

    def test_baseline_store_history(self, tmp_path):
        store = BaselineStore(tmp_path / "baseline.json")
        for i in range(3):
            record = BaselineRecord(
                baseline_id=store.next_id("test.cap"),
                baseline_version="1.0", capability_id="test.cap",
                recorded_at=f"2026-01-0{i+1}T00:00:00Z",
                machine_profile={"python": "3.11"},
                python_metrics={"wall_p50": 0.1 + i * 0.01},
                native_metrics=None, hotspot_class="cpu_bound",
                verdict="OPTIMIZE_PYTHON",
            )
            store.record(record)
        history = store.history("test.cap")
        assert len(history) == 3
        assert history[0].baseline_id == "test.cap-v0001"
        assert history[2].baseline_id == "test.cap-v0003"


class TestRegression:
    def _make_record(self, bid, wall_p50=0.1, wall_p95=0.2, wall_p99=0.3,
                     mem=1000, alloc=100):
        return BaselineRecord(
            baseline_id=bid, baseline_version="1.0",
            capability_id="test", recorded_at="t",
            machine_profile={},
            python_metrics={"wall_p50": wall_p50, "wall_p95": wall_p95,
                            "wall_p99": wall_p99, "peak_memory_p50": mem,
                            "allocation_p50": alloc},
            native_metrics=None, hotspot_class="cpu_bound", verdict="KEEP_PYTHON",
        )

    def test_no_regression_when_improved(self):
        before = self._make_record("v1")
        after = self._make_record("v2", wall_p50=0.08, wall_p95=0.15,
                                  wall_p99=0.25, mem=900, alloc=90)
        evidence = compare_baselines(before, after)
        assert not evidence.is_regression

    def test_regression_when_wall_p50_worsens(self):
        before = self._make_record("v1")
        after = self._make_record("v2", wall_p50=0.15)
        evidence = compare_baselines(before, after)
        assert evidence.is_regression
        assert any("wall_p50" in r for r in evidence.regression_reasons)

    def test_detect_regression_helper(self):
        before = self._make_record("v1")
        after = self._make_record("v2", wall_p50=0.2)
        assert detect_regression(before, after)


class TestParserBenchmark:
    def test_python_token_estimate_basic(self):
        assert python_token_estimate("hello world") == 2
        assert python_token_estimate("") == 0
        assert python_token_estimate("one, two; three.") == 6

    def test_python_chunk_text_small(self):
        text = "word " * 10
        chunks = python_chunk_text(text, chunk_size=512, overlap=64)
        assert len(chunks) == 1

    def test_python_chunk_text_large(self):
        text = "word " * 1000
        chunks = python_chunk_text(text, chunk_size=100, overlap=20)
        assert len(chunks) > 1

    def test_run_parser_benchmark_returns_results(self):
        result = run_parser_benchmark(sample_count=2, warmup_count=1)
        assert result["capability_id"] == "native.parser.token_estimate"
        assert len(result["results"]) > 0
        assert result["classification"] in BOTTLENECK_CLASSES
        assert result["verdict"] in (KEEP_PYTHON, OPTIMIZE_PYTHON, EXPERIMENTAL_NATIVE, NATIVE_CANDIDATE)


class TestVectorBenchmark:
    def test_python_dot_basic(self):
        assert python_dot([1.0, 2.0, 3.0], [4.0, 5.0, 6.0]) == 32.0

    def test_python_cosine_similarity_orthogonal(self):
        result = python_cosine_similarity([1.0, 0.0], [0.0, 1.0])
        assert abs(result) < 0.0001

    def test_python_cosine_similarity_identical(self):
        result = python_cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert abs(result - 1.0) < 0.0001

    def test_python_l2_norm(self):
        assert abs(python_l2_norm([3.0, 4.0]) - 5.0) < 0.0001

    def test_run_vector_benchmark_returns_results(self):
        result = run_vector_benchmark(sample_count=2, warmup_count=1)
        assert result["capability_id"] == "native.vector.similarity"
        assert len(result["results"]) > 0
        assert result["classification"] in BOTTLENECK_CLASSES
        assert result["verdict"] in (KEEP_PYTHON, OPTIMIZE_PYTHON, EXPERIMENTAL_NATIVE, NATIVE_CANDIDATE)


class TestTransformerBenchmark:
    def test_python_matmul_basic(self):
        a = [[1.0, 2.0], [3.0, 4.0]]
        b = [[5.0, 6.0], [7.0, 8.0]]
        result = python_matmul(a, b)
        assert result == [[19.0, 22.0], [43.0, 50.0]]

    def test_python_softmax_sums_to_one(self):
        result = python_softmax([1.0, 2.0, 3.0])
        assert abs(sum(result) - 1.0) < 0.0001
        assert result[2] > result[1] > result[0]

    def test_python_attention_output_dim(self):
        query = [1.0] * 4
        keys = [[1.0] * 4 for _ in range(3)]
        values = [[1.0] * 4 for _ in range(3)]
        output = python_attention(query, keys, values)
        assert len(output) == 4

    def test_run_transformer_benchmark_returns_results(self):
        result = run_transformer_benchmark(sample_count=2, warmup_count=1, compare_numpy=False)
        assert result["capability_id"] == "native.transformer.attention"
        assert len(result["results"]) > 0
        assert result["classification"] in BOTTLENECK_CLASSES
        assert result["verdict"] in (KEEP_PYTHON, OPTIMIZE_PYTHON, EXPERIMENTAL_NATIVE, NATIVE_CANDIDATE)


class TestBaselineJsonFile:
    def test_baseline_json_exists(self):
        assert _BASELINE_PATH.is_file(), f"Baseline JSON missing: {_BASELINE_PATH}"

    def test_baseline_json_valid_structure(self):
        data = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        assert "baseline_id" in data
        assert "baseline_version" in data
        assert "recorded_at" in data
        assert "machine_profile" in data
        assert "benchmarks" in data

    def test_baseline_json_has_three_benchmarks(self):
        data = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        caps = list(data["benchmarks"].keys())
        assert "native.parser.token_estimate" in caps
        assert "native.vector.similarity" in caps
        assert "native.transformer.attention" in caps

    def test_baseline_json_each_benchmark_has_verdict(self):
        data = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        for cap_id, result in data["benchmarks"].items():
            assert "classification" in result, f"{cap_id} missing classification"
            assert "verdict" in result, f"{cap_id} missing verdict"
            assert result["verdict"] in (
                KEEP_PYTHON, OPTIMIZE_PYTHON, EXPERIMENTAL_NATIVE, NATIVE_CANDIDATE,
            ), f"{cap_id} invalid verdict: {result['verdict']}"
            assert "results" in result
            assert len(result["results"]) > 0

    def test_baseline_json_machine_profile_complete(self):
        data = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
        mp = data["machine_profile"]
        assert "python_version" in mp
        assert "platform" in mp
        assert "cpu_count" in mp
        assert "memory_total_bytes" in mp
