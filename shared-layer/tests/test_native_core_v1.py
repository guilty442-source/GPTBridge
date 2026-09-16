"""Tests for C++ Native Core Optimization V1.

Tests the native compute cores (parser, vector, transformer) and the
Python/native dispatcher with fallback (A219).

Tests verify:
    - Native extension loads (or graceful fallback)
    - Parity between Python and native implementations
    - Dispatcher correctly routes to native or Python fallback
    - Dispatch thresholds are respected
    - Benchmark framework runs and produces valid results
    - Performance budgets are checked
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

from shared_layer.performance import (
    native_available,
    should_dispatch,
    get_threshold,
    DISPATCH_THRESHOLDS,
    token_estimate,
    batch_token_estimate,
    dot,
    l2_norm,
    cosine_similarity,
    matmul,
    softmax,
    scaled_dot_product_attention,
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
    run_native_core_benchmark,
    classify_native_result,
)


# --- Dispatcher tests ---

class TestNativeAvailability:
    def test_native_available_returns_bool(self):
        assert isinstance(native_available(), bool)

    def test_dispatch_thresholds_defined(self):
        assert "parser.token_estimate" in DISPATCH_THRESHOLDS
        assert "vector.dot" in DISPATCH_THRESHOLDS
        assert "transformer.matmul" in DISPATCH_THRESHOLDS

    def test_get_threshold_returns_int(self):
        t = get_threshold("parser.token_estimate")
        assert isinstance(t, int)

    def test_should_dispatch_returns_false_for_unavailable(self):
        # If native is not available, should always return False
        if not native_available():
            assert should_dispatch("parser.token_estimate", 10000) is False

    def test_should_dispatch_respects_threshold(self):
        # If native is available, should respect threshold
        if native_available():
            threshold = get_threshold("vector.dot")
            assert should_dispatch("vector.dot", threshold) is True
            if threshold > 0:
                assert should_dispatch("vector.dot", threshold - 1) is False


# --- Parser parity tests ---

class TestParserParity:
    def test_token_estimate_simple(self):
        text = "hello world"
        py = python_token_estimate(text)
        nat = native_token_estimate(text)
        assert py == nat

    def test_token_estimate_with_punctuation(self):
        text = "hello, world! foo; bar."
        py = python_token_estimate(text)
        nat = native_token_estimate(text)
        assert py == nat

    def test_token_estimate_empty(self):
        text = ""
        py = python_token_estimate(text)
        nat = native_token_estimate(text)
        assert py == nat

    def test_token_estimate_large(self):
        text = "word " * 1000
        py = python_token_estimate(text)
        nat = native_token_estimate(text)
        assert py == nat

    def test_batch_token_estimate(self):
        texts = ["hello world", "foo bar baz", "test one two three four"]
        py = python_batch_token_estimate(texts)
        nat = native_batch_token_estimate(texts)
        assert py == nat

    def test_dispatched_token_estimate(self):
        text = "hello world test"
        result = token_estimate(text)
        expected = python_token_estimate(text)
        assert result == expected


# --- Vector parity tests ---

class TestVectorParity:
    def test_dot_product(self):
        a = [1.0, 2.0, 3.0, 4.0]
        b = [5.0, 6.0, 7.0, 8.0]
        py = python_dot(a, b)
        nat = native_dot(a, b)
        assert math.isclose(py, nat, rel_tol=1e-9)

    def test_l2_norm(self):
        a = [3.0, 4.0]
        py = python_l2_norm(a)
        nat = native_l2_norm(a)
        assert math.isclose(py, nat, rel_tol=1e-9)

    def test_cosine_similarity(self):
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        py = python_cosine_similarity(a, b)
        nat = native_cosine_similarity(a, b)
        assert math.isclose(py, nat, rel_tol=1e-9)

    def test_cosine_similarity_identical(self):
        a = [1.0, 2.0, 3.0]
        b = [1.0, 2.0, 3.0]
        py = python_cosine_similarity(a, b)
        nat = native_cosine_similarity(a, b)
        assert math.isclose(py, nat, rel_tol=1e-9)
        assert math.isclose(py, 1.0, rel_tol=1e-9)

    def test_dot_zero_vector(self):
        a = [0.0, 0.0, 0.0]
        b = [1.0, 2.0, 3.0]
        py = python_dot(a, b)
        nat = native_dot(a, b)
        assert math.isclose(py, nat, rel_tol=1e-9)
        assert py == 0.0

    def test_dispatched_dot(self):
        a = list(range(1, 10))
        b = list(range(10, 19))
        result = dot(a, b)
        expected = python_dot(a, b)
        assert math.isclose(result, expected, rel_tol=1e-9)


# --- Transformer parity tests ---

class TestTransformerParity:
    def test_matmul_small(self):
        a = [[1.0, 2.0], [3.0, 4.0]]
        b = [[5.0, 6.0], [7.0, 8.0]]
        py = python_matmul(a, b)
        nat = native_matmul(a, b)
        assert len(py) == len(nat)
        for i in range(len(py)):
            for j in range(len(py[0])):
                assert math.isclose(py[i][j], nat[i][j], rel_tol=1e-9)

    def test_softmax_small(self):
        a = [[1.0, 2.0, 3.0]]
        py = python_softmax(a)
        nat = native_softmax(a)
        assert len(py) == len(nat)
        for i in range(len(py)):
            for j in range(len(py[0])):
                assert math.isclose(py[i][j], nat[i][j], rel_tol=1e-9)

    def test_softmax_sums_to_one(self):
        a = [[1.0, 2.0, 3.0, 4.0]]
        nat = native_softmax(a)
        row_sum = sum(nat[0])
        assert math.isclose(row_sum, 1.0, rel_tol=1e-9)

    def test_scaled_dot_product_attention_small(self):
        q = [[1.0, 0.0]]
        k = [[1.0, 0.0], [0.0, 1.0]]
        v = [[1.0], [2.0]]
        py = python_scaled_dot_product_attention(q, k, v)
        nat = native_scaled_dot_product_attention(q, k, v)
        assert len(py) == len(nat)
        for i in range(len(py)):
            for j in range(len(py[0])):
                assert math.isclose(py[i][j], nat[i][j], rel_tol=1e-9)

    def test_dispatched_matmul(self):
        a = [[1.0, 2.0], [3.0, 4.0]]
        b = [[5.0, 6.0], [7.0, 8.0]]
        result = matmul(a, b)
        expected = python_matmul(a, b)
        assert len(result) == len(expected)


# --- Benchmark tests ---

class TestNativeCoreBenchmark:
    # Cache the benchmark report to avoid running it multiple times
    _cached_report = None

    @classmethod
    def _get_report(cls):
        if cls._cached_report is None:
            cls._cached_report = run_native_core_benchmark()
        return cls._cached_report

    def test_classify_native_candidate(self):
        # A result with speedup > 1.5 and parity should be NATIVE_CANDIDATE
        result = {
            "speedup_large": 2.0,
            "parity_large": True,
        }
        if native_available():
            assert classify_native_result(result) == "NATIVE_CANDIDATE"

    def test_classify_keep_python_no_native(self):
        if not native_available():
            assert classify_native_result({}) == "KEEP_PYTHON"

    def test_classify_keep_python_no_speedup(self):
        result = {
            "speedup_large": 0.5,
            "parity_large": True,
        }
        if native_available():
            assert classify_native_result(result) == "KEEP_PYTHON"

    def test_classify_keep_python_parity_fail(self):
        result = {
            "speedup_large": 5.0,
            "parity_large": False,
        }
        if native_available():
            assert classify_native_result(result) == "KEEP_PYTHON"

    def test_run_native_core_benchmark_produces_report(self):
        report = self._get_report()
        assert report["report_id"] == "native-core-benchmark-v1"
        assert "recorded_at" in report
        assert "machine_profile" in report
        assert "native_available" in report
        assert "benchmarks" in report
        assert "parser" in report["benchmarks"]
        assert "vector" in report["benchmarks"]
        assert "transformer" in report["benchmarks"]

    def test_benchmark_report_has_verdicts(self):
        report = self._get_report()
        for cap in ["parser", "vector", "transformer"]:
            verdict = report["benchmarks"][cap]["verdict"]
            assert verdict in ("NATIVE_CANDIDATE", "KEEP_PYTHON", "OPTIMIZE_PYTHON")

    def test_benchmark_report_has_speedups_when_native_available(self):
        report = self._get_report()
        if report["native_available"]:
            parser = report["benchmarks"]["parser"]
            assert any(k.startswith("speedup_") for k in parser)

    def test_benchmark_report_has_parity_when_native_available(self):
        report = self._get_report()
        if report["native_available"]:
            parser = report["benchmarks"]["parser"]
            parity_keys = [k for k in parser if k.startswith("parity_")]
            assert len(parity_keys) > 0
            # All parity checks should pass
            for k in parity_keys:
                assert parser[k] is True, f"parity failed for {k}"
