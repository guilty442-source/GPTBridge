"""Tests for Performance Budget & Regression Control V1.

Tests the full regression control pipeline:
    - Versioned PerformanceBaseline with full metrics
    - PERF_SMOKE / PERF_STANDARD / PERF_FULL tiers
    - Statistical tolerance from multiple stable runs
    - Comparability verification + COMPARISON_INVALID
    - Historical trend + regression attribution
    - PERFORMANCE_STABLE marking
    - Baseline update policy (no auto-reset)

Correctness/contract/resource safety always overrides performance.
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

import pytest

from shared_layer.performance import (
    # V2 baseline
    PERF_BASELINE_VERSION,
    EnvironmentProfile,
    capture_environment_profile,
    WorkloadProfile,
    FullMetrics,
    PerformanceBaselineRecord,
    PerformanceBaselineStore,
    # Tiers
    BenchmarkTier,
    TierConfig,
    TIER_CONFIGS,
    get_tier_config,
    select_tier_by_env,
    # Tolerance
    MetricTolerance,
    ToleranceSet,
    ToleranceCheckResult,
    compute_tolerance,
    compute_tolerance_set,
    check_tolerance,
    check_all_tolerances,
    LOWER_IS_BETTER,
    HIGHER_IS_BETTER,
    # Comparison
    ComparabilityResult,
    ComparisonResult,
    verify_comparability,
    compare_against_baseline,
    CRITICAL_ENV_FIELDS,
    SOFT_ENV_FIELDS,
    # Trend
    MetricTrend,
    RegressionAttribution,
    OperationTrend,
    compute_metric_trend,
    compute_operation_trend,
    attribute_regression,
    METRIC_BOUNDARY_MAP,
    # Stability
    StabilityCheck,
    StabilityResult,
    STABILITY_BUDGETS,
    check_stability_budgets,
    assess_stability,
    # Regression control
    RegressionControlReport,
    RegressionController,
    write_regression_control_report,
)


# ---------------------------------------------------------------------------
# Environment Profile
# ---------------------------------------------------------------------------

class TestEnvironmentProfile:
    def test_capture_environment_profile(self):
        env = capture_environment_profile()
        assert env.python_version
        assert env.platform
        assert env.cpu_count > 0
        assert env.memory_total_bytes > 0
        assert env.baseline_tool_version == PERF_BASELINE_VERSION

    def test_environment_profile_is_frozen(self):
        env = capture_environment_profile()
        with pytest.raises(AttributeError):
            env.python_version = "3.12"

    def test_environment_profile_to_dict(self):
        env = EnvironmentProfile(
            python_version="3.11",
            platform="Windows",
            processor="x86_64",
            cpu_count=8,
            memory_total_bytes=16 * 1024**3,
            machine="AMD64",
        )
        d = env.__dict__
        assert d["python_version"] == "3.11"
        assert d["cpu_count"] == 8


# ---------------------------------------------------------------------------
# Benchmark Tiers
# ---------------------------------------------------------------------------

class TestBenchmarkTiers:
    def test_three_tiers_exist(self):
        assert BenchmarkTier.SMOKE.value == "PERF_SMOKE"
        assert BenchmarkTier.STANDARD.value == "PERF_STANDARD"
        assert BenchmarkTier.FULL.value == "PERF_FULL"

    def test_tier_configs(self):
        smoke = get_tier_config(BenchmarkTier.SMOKE)
        assert "small" in smoke.sizes
        assert "warm" in smoke.warmths
        assert smoke.sample_count <= 10

        standard = get_tier_config(BenchmarkTier.STANDARD)
        assert "medium" in standard.sizes
        assert "cold" in standard.warmths
        assert standard.sample_count > smoke.sample_count

        full = get_tier_config(BenchmarkTier.FULL)
        assert "large" in full.sizes
        assert full.sample_count > standard.sample_count
        assert 8 in full.concurrencies

    def test_smoke_is_fastest(self):
        smoke = get_tier_config(BenchmarkTier.SMOKE)
        standard = get_tier_config(BenchmarkTier.STANDARD)
        full = get_tier_config(BenchmarkTier.FULL)
        assert smoke.sample_count <= standard.sample_count <= full.sample_count
        assert len(smoke.sizes) <= len(standard.sizes) <= len(full.sizes)

    def test_select_tier_by_env_dev(self):
        assert select_tier_by_env("dev") == BenchmarkTier.SMOKE
        assert select_tier_by_env("development") == BenchmarkTier.SMOKE
        assert select_tier_by_env(None) == BenchmarkTier.SMOKE

    def test_select_tier_by_env_ci(self):
        assert select_tier_by_env("ci") == BenchmarkTier.STANDARD
        assert select_tier_by_env("integration") == BenchmarkTier.STANDARD

    def test_select_tier_by_env_release(self):
        assert select_tier_by_env("release") == BenchmarkTier.FULL
        assert select_tier_by_env("production") == BenchmarkTier.FULL

    def test_full_covers_all_sizes_and_concurrencies(self):
        full = get_tier_config(BenchmarkTier.FULL)
        assert len(full.sizes) == 3
        assert len(full.warmths) == 2
        assert len(full.concurrencies) >= 2


# ---------------------------------------------------------------------------
# Statistical Tolerance
# ---------------------------------------------------------------------------

class TestTolerance:
    def test_compute_tolerance_statistical(self):
        values = [1.0, 1.05, 0.95, 1.02, 0.98, 1.01, 0.99]
        tol = compute_tolerance("wall_p50", values)
        assert tol.method == "statistical"
        assert tol.sample_count == 7
        assert tol.mean > 0.95
        assert tol.std_dev > 0
        assert tol.warning_threshold > tol.mean
        assert tol.fail_threshold > tol.warning_threshold

    def test_compute_tolerance_fallback_insufficient_data(self):
        values = [1.0]
        tol = compute_tolerance("wall_p50", values)
        assert tol.method == "fallback"
        assert tol.sample_count == 1
        # Fallback uses fixed percentage
        assert tol.warning_threshold == 1.0 * 1.25
        assert tol.fail_threshold == 1.0 * 1.50

    def test_compute_tolerance_empty(self):
        tol = compute_tolerance("wall_p50", [])
        assert tol.method == "fallback"
        assert tol.mean == 0.0

    def test_compute_tolerance_higher_is_better(self):
        values = [100.0, 105.0, 95.0, 102.0, 98.0]
        tol = compute_tolerance("throughput_p50", values)
        assert tol.direction == "higher_is_better"
        # For higher-is-better, thresholds are BELOW mean
        assert tol.warning_threshold < tol.mean
        assert tol.fail_threshold < tol.warning_threshold

    def test_compute_tolerance_lower_is_better(self):
        values = [1.0, 1.05, 0.95, 1.02, 0.98]
        tol = compute_tolerance("wall_p50", values)
        assert tol.direction == "lower_is_better"
        assert tol.warning_threshold > tol.mean
        assert tol.fail_threshold > tol.warning_threshold

    def test_check_tolerance_pass(self):
        tol = MetricTolerance(
            metric_name="wall_p50",
            mean=1.0,
            std_dev=0.05,
            warning_threshold=1.1,
            fail_threshold=1.15,
            sample_count=5,
            method="statistical",
            direction="lower_is_better",
        )
        result = check_tolerance(tol, 1.0)
        assert result.status == "PASS"

    def test_check_tolerance_warn(self):
        tol = MetricTolerance(
            metric_name="wall_p50",
            mean=1.0,
            std_dev=0.05,
            warning_threshold=1.1,
            fail_threshold=1.15,
            sample_count=5,
            method="statistical",
            direction="lower_is_better",
        )
        result = check_tolerance(tol, 1.12)
        assert result.status == "WARN"

    def test_check_tolerance_fail(self):
        tol = MetricTolerance(
            metric_name="wall_p50",
            mean=1.0,
            std_dev=0.05,
            warning_threshold=1.1,
            fail_threshold=1.15,
            sample_count=5,
            method="statistical",
            direction="lower_is_better",
        )
        result = check_tolerance(tol, 1.2)
        assert result.status == "FAIL"

    def test_check_tolerance_higher_is_better_pass(self):
        tol = MetricTolerance(
            metric_name="throughput_p50",
            mean=100.0,
            std_dev=5.0,
            warning_threshold=90.0,
            fail_threshold=85.0,
            sample_count=5,
            method="statistical",
            direction="higher_is_better",
        )
        result = check_tolerance(tol, 100.0)
        assert result.status == "PASS"

    def test_check_tolerance_higher_is_better_fail(self):
        tol = MetricTolerance(
            metric_name="throughput_p50",
            mean=100.0,
            std_dev=5.0,
            warning_threshold=90.0,
            fail_threshold=85.0,
            sample_count=5,
            method="statistical",
            direction="higher_is_better",
        )
        result = check_tolerance(tol, 80.0)
        assert result.status == "FAIL"

    def test_compute_tolerance_set(self):
        metric_runs = {
            "wall_p50": [1.0, 1.05, 0.95, 1.02, 0.98],
            "peak_memory_p50": [1024.0, 1100.0, 980.0, 1050.0, 1020.0],
        }
        ts = compute_tolerance_set("test_op", metric_runs)
        assert ts.operation_id == "test_op"
        assert "wall_p50" in ts.tolerances
        assert "peak_memory_p50" in ts.tolerances
        assert ts.method == "statistical"

    def test_compute_tolerance_set_mixed_methods(self):
        metric_runs = {
            "wall_p50": [1.0, 1.05, 0.95, 1.02, 0.98],
            "new_metric": [1.0],  # insufficient
        }
        ts = compute_tolerance_set("test_op", metric_runs)
        assert ts.method == "fallback"  # because new_metric used fallback

    def test_lower_is_better_set(self):
        assert "wall_p50" in LOWER_IS_BETTER
        assert "peak_memory_p50" in LOWER_IS_BETTER
        assert "sql_query_count" in LOWER_IS_BETTER
        assert "python_native_crossings" in LOWER_IS_BETTER

    def test_higher_is_better_set(self):
        assert "throughput_p50" in HIGHER_IS_BETTER


# ---------------------------------------------------------------------------
# Comparability Verification
# ---------------------------------------------------------------------------

class TestComparability:
    def _make_env(self, **kwargs):
        defaults = {
            "python_version": "3.11",
            "platform": "Windows",
            "processor": "x86_64",
            "cpu_count": 8,
            "memory_total_bytes": 16 * 1024**3,
            "machine": "AMD64",
        }
        defaults.update(kwargs)
        return defaults

    def test_comparable_environments(self):
        env1 = self._make_env()
        env2 = self._make_env()
        result = verify_comparability(env1, env2)
        assert result.comparable
        assert result.status == "COMPARABLE"
        assert len(result.mismatched_fields) == 0

    def test_comparison_invalid_python_version(self):
        env1 = self._make_env(python_version="3.11")
        env2 = self._make_env(python_version="3.12")
        result = verify_comparability(env1, env2)
        assert not result.comparable
        assert result.status == "COMPARISON_INVALID"
        assert "python_version" in result.mismatched_fields

    def test_comparison_invalid_platform(self):
        env1 = self._make_env(platform="Windows")
        env2 = self._make_env(platform="Linux")
        result = verify_comparability(env1, env2)
        assert result.status == "COMPARISON_INVALID"
        assert "platform" in result.mismatched_fields

    def test_comparison_invalid_cpu_count(self):
        env1 = self._make_env(cpu_count=8)
        env2 = self._make_env(cpu_count=16)
        result = verify_comparability(env1, env2)
        assert result.status == "COMPARISON_INVALID"
        assert "cpu_count" in result.mismatched_fields

    def test_soft_mismatch_compiler(self):
        env1 = self._make_env(compiler="MSVC 14.51")
        env2 = self._make_env(compiler="MSVC 14.52")
        result = verify_comparability(env1, env2)
        assert result.comparable
        assert result.status == "COMPARABLE"
        assert "compiler" in result.soft_mismatches

    def test_soft_mismatch_numpy(self):
        env1 = self._make_env(numpy_version="1.24")
        env2 = self._make_env(numpy_version="1.25")
        result = verify_comparability(env1, env2)
        assert result.comparable
        assert "numpy_version" in result.soft_mismatches

    def test_critical_env_fields(self):
        assert "python_version" in CRITICAL_ENV_FIELDS
        assert "platform" in CRITICAL_ENV_FIELDS
        assert "cpu_count" in CRITICAL_ENV_FIELDS
        assert "machine" in CRITICAL_ENV_FIELDS

    def test_soft_env_fields(self):
        assert "processor" in SOFT_ENV_FIELDS
        assert "compiler" in SOFT_ENV_FIELDS
        assert "numpy_version" in SOFT_ENV_FIELDS


# ---------------------------------------------------------------------------
# Trend and Attribution
# ---------------------------------------------------------------------------

class TestTrend:
    def _make_baseline(self, op_id, metrics, seq=1):
        return PerformanceBaselineRecord(
            baseline_id=f"{op_id}-v{seq:04d}",
            baseline_version="2.0",
            operation_id=op_id,
            revision="",
            recorded_at=f"2024-01-{seq:02d}T00:00:00Z",
            environment={},
            workload={},
            metrics=metrics,
            hotspot_class="cpu_bound",
            verdict="NATIVE_CANDIDATE",
            stability="UNKNOWN",
        )

    def test_compute_metric_trend_improving(self):
        history = [
            self._make_baseline("op", {"wall_p50": 2.0}, 1),
            self._make_baseline("op", {"wall_p50": 1.8}, 2),
            self._make_baseline("op", {"wall_p50": 1.5}, 3),
        ]
        trend = compute_metric_trend("wall_p50", history)
        assert trend.direction == "improving"
        assert trend.total_delta_pct < 0

    def test_compute_metric_trend_regressing(self):
        history = [
            self._make_baseline("op", {"wall_p50": 1.0}, 1),
            self._make_baseline("op", {"wall_p50": 1.2}, 2),
            self._make_baseline("op", {"wall_p50": 1.5}, 3),
        ]
        trend = compute_metric_trend("wall_p50", history)
        assert trend.direction == "regressing"
        assert trend.total_delta_pct > 0

    def test_compute_metric_trend_stable(self):
        history = [
            self._make_baseline("op", {"wall_p50": 1.0}, 1),
            self._make_baseline("op", {"wall_p50": 1.01}, 2),
            self._make_baseline("op", {"wall_p50": 1.02}, 3),
        ]
        trend = compute_metric_trend("wall_p50", history)
        assert trend.direction == "stable"

    def test_compute_metric_trend_insufficient_data(self):
        history = [self._make_baseline("op", {"wall_p50": 1.0}, 1)]
        trend = compute_metric_trend("wall_p50", history)
        assert trend.direction == "unknown"

    def test_compute_operation_trend(self):
        history = [
            self._make_baseline("op", {"wall_p50": 2.0, "peak_memory_p50": 1024.0}, 1),
            self._make_baseline("op", {"wall_p50": 1.8, "peak_memory_p50": 1000.0}, 2),
            self._make_baseline("op", {"wall_p50": 1.5, "peak_memory_p50": 980.0}, 3),
        ]
        trend = compute_operation_trend("op", history)
        assert trend.operation_id == "op"
        assert trend.baseline_count == 3
        assert "wall_p50" in trend.metric_trends
        assert trend.overall_direction == "improving"

    def test_compute_operation_trend_empty(self):
        trend = compute_operation_trend("op", [])
        assert trend.baseline_count == 0
        assert trend.overall_direction == "unknown"

    def test_attribute_regression(self):
        attr = attribute_regression("op", "wall_p50", 1.0, 1.5, "WARN")
        assert attr.operation_id == "op"
        assert attr.metric_name == "wall_p50"
        assert attr.baseline_value == 1.0
        assert attr.current_value == 1.5
        assert attr.delta_pct == 50.0
        assert attr.dominant_boundary == "python"
        assert attr.status == "WARN"

    def test_attribute_regression_sql(self):
        attr = attribute_regression("op", "sql_query_count", 5, 15, "FAIL")
        assert attr.dominant_boundary == "sql"

    def test_attribute_regression_native(self):
        attr = attribute_regression("op", "native_copy_bytes", 100, 500, "FAIL")
        assert attr.dominant_boundary == "native"

    def test_metric_boundary_map(self):
        assert METRIC_BOUNDARY_MAP["wall_p50"] == "python"
        assert METRIC_BOUNDARY_MAP["sql_query_count"] == "sql"
        assert METRIC_BOUNDARY_MAP["python_native_crossings"] == "native"
        assert METRIC_BOUNDARY_MAP["model_wait_ms"] == "model"


# ---------------------------------------------------------------------------
# Stability
# ---------------------------------------------------------------------------

class TestStability:
    def _make_baseline(self, metrics, stability="UNKNOWN"):
        return PerformanceBaselineRecord(
            baseline_id="op-v0001",
            baseline_version="2.0",
            operation_id="op",
            revision="",
            recorded_at="2024-01-01T00:00:00Z",
            environment={},
            workload={},
            metrics=metrics,
            hotspot_class="cpu_bound",
            verdict="NATIVE_CANDIDATE",
            stability=stability,
        )

    def test_check_stability_budgets_all_pass(self):
        metrics = {
            "wall_p50": 1.0,
            "wall_p95": 5.0,
            "wall_p99": 10.0,
            "peak_memory_p50": 1024 * 1024,
            "retained_memory_p50": 512 * 1024,
            "python_native_crossings": 10,
            "ts_python_crossings": 5,
            "sql_query_count": 5,
            "cpu_p50": 1.0,
            "allocation_count": 10000,
        }
        checks = check_stability_budgets(metrics)
        assert len(checks) > 0
        assert all(c.passed for c in checks)

    def test_check_stability_budgets_fail(self):
        metrics = {
            "wall_p50": 100.0,  # exceeds max of 10
            "wall_p95": 5.0,
            "wall_p99": 10.0,
            "peak_memory_p50": 1024 * 1024,
            "retained_memory_p50": 512 * 1024,
            "python_native_crossings": 10,
            "ts_python_crossings": 5,
            "sql_query_count": 5,
            "cpu_p50": 1.0,
            "allocation_count": 10000,
        }
        checks = check_stability_budgets(metrics)
        wall_check = [c for c in checks if c.metric_name == "wall_p50"][0]
        assert not wall_check.passed

    def test_assess_stability_stable(self):
        metrics = {
            "wall_p50": 1.0,
            "wall_p95": 5.0,
            "wall_p99": 10.0,
            "peak_memory_p50": 1024 * 1024,
            "retained_memory_p50": 512 * 1024,
            "python_native_crossings": 10,
            "ts_python_crossings": 5,
            "sql_query_count": 5,
            "cpu_p50": 1.0,
            "allocation_count": 10000,
        }
        baseline = self._make_baseline(metrics)
        result = assess_stability(baseline, correctness_passed=True)
        assert result.stability == "PERFORMANCE_STABLE"

    def test_assess_stability_unstable_budget(self):
        metrics = {
            "wall_p50": 100.0,  # exceeds max
            "wall_p95": 5.0,
            "wall_p99": 10.0,
            "peak_memory_p50": 1024 * 1024,
            "retained_memory_p50": 512 * 1024,
            "python_native_crossings": 10,
            "ts_python_crossings": 5,
            "sql_query_count": 5,
            "cpu_p50": 1.0,
            "allocation_count": 10000,
        }
        baseline = self._make_baseline(metrics)
        result = assess_stability(baseline, correctness_passed=True)
        assert result.stability == "UNSTABLE"

    def test_assess_stability_correctness_overrides(self):
        metrics = {
            "wall_p50": 1.0,
            "wall_p95": 5.0,
            "wall_p99": 10.0,
            "peak_memory_p50": 1024 * 1024,
            "retained_memory_p50": 512 * 1024,
            "python_native_crossings": 10,
            "ts_python_crossings": 5,
            "sql_query_count": 5,
            "cpu_p50": 1.0,
            "allocation_count": 10000,
        }
        baseline = self._make_baseline(metrics)
        result = assess_stability(baseline, correctness_passed=False)
        assert result.stability == "UNSTABLE"
        assert "correctness" in result.message.lower()

    def test_stability_budgets_defined(self):
        assert "wall_p50" in STABILITY_BUDGETS
        assert "wall_p95" in STABILITY_BUDGETS
        assert "wall_p99" in STABILITY_BUDGETS
        assert "peak_memory_p50" in STABILITY_BUDGETS
        assert "python_native_crossings" in STABILITY_BUDGETS
        assert "sql_query_count" in STABILITY_BUDGETS
        assert "cpu_p50" in STABILITY_BUDGETS
        assert "allocation_count" in STABILITY_BUDGETS


# ---------------------------------------------------------------------------
# Regression Controller
# ---------------------------------------------------------------------------

class TestRegressionController:
    def _make_env_dict(self, **kwargs):
        defaults = {
            "python_version": "3.11",
            "platform": "Windows",
            "processor": "x86_64",
            "cpu_count": 8,
            "memory_total_bytes": 16 * 1024**3,
            "machine": "AMD64",
            "compiler": "MSVC 14.51",
            "numpy_version": "1.24",
            "pybind11_version": "2.13.6",
        }
        defaults.update(kwargs)
        return defaults

    def _make_metrics(self, **kwargs):
        defaults = {
            "wall_p50": 1.0,
            "wall_p95": 5.0,
            "wall_p99": 10.0,
            "cpu_p50": 1.0,
            "cpu_p95": 5.0,
            "cpu_p99": 10.0,
            "throughput_p50": 100.0,
            "peak_memory_p50": 1024 * 1024,
            "retained_memory_p50": 512 * 1024,
            "sql_query_count": 5,
            "sql_row_count": 100,
            "sql_byte_count": 1024,
            "ts_python_crossings": 5,
            "python_native_crossings": 10,
            "python_csharp_crossings": 0,
            "serialization_bytes": 1024,
            "serialization_count": 5,
            "native_copy_bytes": 512,
            "native_allocation_count": 100,
            "queue_wait_ms": 1.0,
            "model_wait_ms": 0.0,
            "call_count": 1000,
            "allocation_count": 10000,
        }
        defaults.update(kwargs)
        return defaults

    def test_first_baseline_recorded(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = self._make_env_dict()
        metrics = self._make_metrics()

        report = controller.run(
            "test.op", env, metrics, correctness_passed=True,
        )
        assert report.operation_id == "test.op"
        assert "first baseline" in report.summary.lower()
        # Baseline should be recorded
        assert store.latest("test.op") is not None

    def test_comparison_comparable(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = self._make_env_dict()
        metrics = self._make_metrics()

        # First run: records baseline
        controller.run("test.op", env, metrics, correctness_passed=True)

        # Second run: compares against baseline
        report = controller.run("test.op", env, metrics, correctness_passed=True)
        assert report.comparability_status == "COMPARABLE"
        assert report.comparison_status == "PASS"

    def test_comparison_invalid_env(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env1 = self._make_env_dict(python_version="3.11")
        metrics = self._make_metrics()

        # First run
        controller.run("test.op", env1, metrics, correctness_passed=True)

        # Second run with different Python version
        env2 = self._make_env_dict(python_version="3.12")
        report = controller.run("test.op", env2, metrics, correctness_passed=True)
        assert report.comparability_status == "COMPARISON_INVALID"
        assert report.comparison_status == "COMPARISON_INVALID"

    def test_correctness_failure_overrides(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = self._make_env_dict()
        metrics = self._make_metrics()

        # First run
        controller.run("test.op", env, metrics, correctness_passed=True)

        # Second run with correctness failure
        report = controller.run("test.op", env, metrics, correctness_passed=False)
        assert report.comparison_status == "FAIL"
        assert not report.correctness_passed

    def test_intentional_baseline_update(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = self._make_env_dict()
        workload = {"size": "medium", "warmth": "warm", "concurrency": 1}
        metrics = self._make_metrics()

        baseline_id = controller.record_intentional_baseline_update(
            "test.op", env, workload, metrics,
            update_reason="optimized chunking algorithm",
        )
        assert baseline_id
        record = store.latest("test.op")
        assert record.intentional_update is True
        assert record.update_reason == "optimized chunking algorithm"

    def test_intentional_update_requires_reason(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = self._make_env_dict()
        workload = {}
        metrics = self._make_metrics()

        with pytest.raises(ValueError, match="reason"):
            controller.record_intentional_baseline_update(
                "test.op", env, workload, metrics,
                update_reason="",
            )

    def test_no_auto_reset_on_regression(self, tmp_path):
        """Baseline must NOT be auto-reset to eliminate a regression."""
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = self._make_env_dict()
        good_metrics = self._make_metrics(wall_p50=1.0)

        # First run: good baseline
        controller.run("test.op", env, good_metrics, correctness_passed=True)

        # Second run: regression (wall_p50 doubled)
        bad_metrics = self._make_metrics(wall_p50=2.0)
        report = controller.run("test.op", env, bad_metrics, correctness_passed=True)

        # The baseline should NOT have been reset
        latest = store.latest("test.op")
        assert latest.metrics["wall_p50"] == 1.0  # original baseline preserved

    def test_write_regression_control_report(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = self._make_env_dict()
        metrics = self._make_metrics()

        report = controller.run("test.op", env, metrics, correctness_passed=True)
        report_path = tmp_path / "report.json"
        write_regression_control_report(report, report_path)
        assert report_path.exists()
        data = json.loads(report_path.read_text(encoding="utf-8"))
        assert data["operation_id"] == "test.op"

    def test_baseline_store_history(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = self._make_env_dict()
        metrics = self._make_metrics()

        # Record multiple baselines
        controller.run("op1", env, metrics, correctness_passed=True)
        controller.run("op2", env, metrics, correctness_passed=True)
        controller.record_intentional_baseline_update(
            "op1", env, {}, metrics, update_reason="test",
        )

        history = store.history("op1")
        assert len(history) == 2
        assert store.all_operations() == ["op1", "op2"]

    def test_baseline_store_next_id(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = self._make_env_dict()
        metrics = self._make_metrics()

        controller.run("op", env, metrics, correctness_passed=True)
        assert store.next_id("op") == "op-v0002"

    def test_tier_selection_in_report(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = self._make_env_dict()
        metrics = self._make_metrics()

        report = controller.run(
            "test.op", env, metrics,
            correctness_passed=True,
            tier=BenchmarkTier.FULL,
        )
        assert report.tier == "PERF_FULL"


# ---------------------------------------------------------------------------
# Full Metrics Coverage
# ---------------------------------------------------------------------------

class TestFullMetrics:
    def test_full_metrics_fields(self):
        m = FullMetrics(
            wall_p50=1.0, wall_p95=5.0, wall_p99=10.0,
            cpu_p50=1.0, cpu_p95=5.0, cpu_p99=10.0,
            throughput_p50=100.0,
            peak_memory_p50=1024, retained_memory_p50=512,
            sql_query_count=5, sql_row_count=100, sql_byte_count=1024,
            ts_python_crossings=5, python_native_crossings=10, python_csharp_crossings=0,
            serialization_bytes=1024, serialization_count=5,
            native_copy_bytes=512, native_allocation_count=100,
            queue_wait_ms=1.0, model_wait_ms=0.0,
            call_count=1000, allocation_count=10000,
        )
        assert m.wall_p50 == 1.0
        assert m.sql_query_count == 5
        assert m.python_native_crossings == 10
        assert m.native_copy_bytes == 512
        assert m.model_wait_ms == 0.0

    def test_workload_profile_fields(self):
        w = WorkloadProfile(
            size_class="medium",
            warmth="warm",
            concurrency=4,
            input_size=1000,
            sample_count=20,
            warmup_count=5,
        )
        assert w.size_class == "medium"
        assert w.concurrency == 4


# ---------------------------------------------------------------------------
# Regression Control Report Structure
# ---------------------------------------------------------------------------

class TestRegressionControlReport:
    def test_report_has_all_fields(self, tmp_path):
        store = PerformanceBaselineStore(tmp_path / "test_baseline.json")
        controller = RegressionController(store)
        env = {
            "python_version": "3.11",
            "platform": "Windows",
            "processor": "x86_64",
            "cpu_count": 8,
            "memory_total_bytes": 16 * 1024**3,
            "machine": "AMD64",
        }
        metrics = {
            "wall_p50": 1.0,
            "wall_p95": 5.0,
            "wall_p99": 10.0,
            "cpu_p50": 1.0,
            "cpu_p95": 5.0,
            "cpu_p99": 10.0,
            "throughput_p50": 100.0,
            "peak_memory_p50": 1024 * 1024,
            "retained_memory_p50": 512 * 1024,
            "sql_query_count": 5,
            "sql_row_count": 100,
            "sql_byte_count": 1024,
            "ts_python_crossings": 5,
            "python_native_crossings": 10,
            "python_csharp_crossings": 0,
            "serialization_bytes": 1024,
            "serialization_count": 5,
            "native_copy_bytes": 512,
            "native_allocation_count": 100,
            "queue_wait_ms": 1.0,
            "model_wait_ms": 0.0,
            "call_count": 1000,
            "allocation_count": 10000,
        }

        report = controller.run("test.op", env, metrics, correctness_passed=True)
        assert report.operation_id
        assert report.tier
        assert report.recorded_at
        assert isinstance(report.metric_results, list)
        assert isinstance(report.regressions, list)
        assert isinstance(report.stability_checks, list)
        assert report.summary
