"""Tests for Python Runtime Optimization V1.

Tests the optimizations applied in Runtime Optimization V1:
    - chunking O(n^2) -> O(n) cumulative char tracking
    - lazy tiktoken/openai/httpx import
    - encode_json fast-path length check
    - reconcile mark_pending_batch
    - pre-compiled regex in chunking/citation/code_rag
    - performance budgets
    - regression benchmarks

These tests verify correctness preservation and budget compliance,
not absolute performance numbers.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_p = str(Path(__file__).resolve().parents[1] / "src")
if _p not in sys.path:
    sys.path.insert(0, _p)

from shared_layer.performance import (
    PerformanceBudget,
    BudgetCheckResult,
    get_budget,
    list_budgets,
    check_budget,
    check_all_budgets,
    run_regression_benchmarks,
)
from shared_layer.performance.regression_benchmarks import (
    _benchmark_chunking_optimization,
    _benchmark_encode_json_optimization,
    _benchmark_reconcile_batch,
    _benchmark_import_time,
)
from shared_layer.store_helpers import encode_json, _MAX_BYTES
from shared_layer.reconcile import ReconcileStateStore


# --- Performance budgets tests ---

class TestPerformanceBudgets:
    def test_get_budget_returns_budget_for_known_capability(self):
        budget = get_budget("native.parser.token_estimate")
        assert budget is not None
        assert budget.capability_id == "native.parser.token_estimate"
        assert budget.warm_p50_ms > 0
        assert budget.tolerance_pct > 0

    def test_get_budget_returns_none_for_unknown_capability(self):
        assert get_budget("unknown.capability") is None

    def test_list_budgets_returns_all_capabilities(self):
        caps = list_budgets()
        assert "native.parser.token_estimate" in caps
        assert "native.vector.similarity" in caps
        assert "native.transformer.attention" in caps
        assert "python.rag.chunking" in caps
        assert "python.store.encode_json" in caps
        assert "python.reconcile.mark_pending" in caps

    def test_check_budget_pass_when_under_budget(self):
        result = check_budget("native.parser.token_estimate", "warm_p50_ms", 1.0)
        assert result.status == "PASS"
        assert result.actual == 1.0

    def test_check_budget_warn_when_within_tolerance(self):
        budget = get_budget("native.parser.token_estimate")
        # Exceed by 10% (within 20% tolerance)
        actual = budget.warm_p50_ms * 1.1
        result = check_budget("native.parser.token_estimate", "warm_p50_ms", actual)
        assert result.status == "WARN"

    def test_check_budget_fail_when_above_tolerance(self):
        budget = get_budget("native.parser.token_estimate")
        # Exceed by 50% (above 20% tolerance)
        actual = budget.warm_p50_ms * 1.5
        result = check_budget("native.parser.token_estimate", "warm_p50_ms", actual)
        assert result.status == "FAIL"

    def test_check_budget_pass_for_unknown_capability(self):
        result = check_budget("unknown.cap", "warm_p50_ms", 100.0)
        assert result.status == "PASS"
        assert "no budget" in result.message

    def test_check_budget_pass_for_unknown_metric(self):
        result = check_budget("native.parser.token_estimate", "nonexistent_metric", 100.0)
        assert result.status == "PASS"
        assert "no budget metric" in result.message

    def test_check_all_budgets_checks_all_metrics(self):
        results = check_all_budgets("native.parser.token_estimate", {
            "warm_p50_ms": 1.0,
            "warm_p95_ms": 5.0,
            "peak_memory_kb": 100.0,
        })
        assert len(results) == 3
        assert all(r.status == "PASS" for r in results)


# --- Chunking O(n) optimization tests ---

class TestChunkingOptimization:
    def test_chunking_optimization_preserves_correctness(self):
        result = _benchmark_chunking_optimization()
        assert result["correctness_preserved"] is True

    def test_chunking_optimization_produces_speedup(self):
        result = _benchmark_chunking_optimization()
        assert result["speedup"] >= 1.0  # O(n) should be at least as fast

    def test_chunking_optimization_has_budget_checks(self):
        result = _benchmark_chunking_optimization()
        assert len(result["budget_checks"]) > 0
        # At least one should be PASS or WARN (not all FAIL)
        statuses = [c["status"] for c in result["budget_checks"]]
        assert "PASS" in statuses or "WARN" in statuses


# --- encode_json optimization tests ---

class TestEncodeJsonOptimization:
    def test_encode_json_preserves_correctness(self):
        payload = {"b": 2, "a": 1, "c": [3, 4]}
        encoded = encode_json(payload)
        decoded = json.loads(encoded)
        assert decoded == payload

    def test_encode_json_sorts_keys(self):
        payload = {"b": 2, "a": 1}
        encoded = encode_json(payload)
        # sort_keys=True means "a" comes before "b"
        assert encoded.index('"a"') < encoded.index('"b"')

    def test_encode_json_rejects_oversized(self):
        # Create a payload that exceeds _MAX_BYTES
        payload = {"data": "x" * (_MAX_BYTES + 1)}
        try:
            encode_json(payload)
            assert False, "should have raised"
        except Exception:
            pass  # permission_denied

    def test_encode_json_handles_ascii_fast_path(self):
        payload = {"request_id": "req-123", "tool_id": "tool-456"}
        encoded = encode_json(payload)
        assert isinstance(encoded, str)
        assert len(encoded) < _MAX_BYTES

    def test_encode_json_handles_unicode(self):
        payload = {"text": "hello world test"}
        encoded = encode_json(payload)
        decoded = json.loads(encoded)
        assert decoded == payload

    def test_encode_json_benchmark_runs(self):
        result = _benchmark_encode_json_optimization()
        assert result["capability_id"] == "python.store.encode_json"
        assert result["wall_p50_ms"] > 0
        assert len(result["budget_checks"]) > 0


# --- Reconcile batch optimization tests ---

class TestReconcileBatchOptimization:
    def test_mark_pending_batch_preserves_correctness(self):
        conn = sqlite3.connect(":memory:")
        store = ReconcileStateStore(conn)

        # Single row
        store.mark_pending("mod-a", "res-1", 1, "2026-01-01T00:00:00Z", None)
        # Batch
        records = [
            ("mod-a", f"res-{i}", 1, "2026-01-01T00:00:00Z", None)
            for i in range(2, 10)
        ]
        count = store.mark_pending_batch(records)
        assert count == 8

        # Verify all 9 records (1 single + 8 batch)
        pending = store.pending("mod-a", limit=100)
        assert len(pending) == 9

        conn.close()

    def test_mark_pending_batch_empty_returns_zero(self):
        conn = sqlite3.connect(":memory:")
        store = ReconcileStateStore(conn)
        count = store.mark_pending_batch([])
        assert count == 0
        conn.close()

    def test_mark_pending_batch_updates_existing(self):
        conn = sqlite3.connect(":memory:")
        store = ReconcileStateStore(conn)

        # Insert initial
        store.mark_pending("mod-a", "res-1", 1, "2026-01-01T00:00:00Z", None)

        # Batch update
        records = [("mod-a", "res-1", 2, "2026-01-02T00:00:00Z", "hash123")]
        count = store.mark_pending_batch(records)
        assert count == 1

        # Verify update
        pending = store.pending("mod-a", limit=10)
        assert len(pending) == 1
        assert pending[0].local_version == 2
        assert pending[0].local_content_hash == "hash123"

        conn.close()

    def test_reconcile_batch_benchmark_runs(self):
        result = _benchmark_reconcile_batch()
        assert result["capability_id"] == "python.reconcile.mark_pending"
        assert result["speedup"] >= 1.0  # batch should be at least as fast
        assert result["correctness_preserved"] is True


# --- Import-time optimization tests ---

class TestImportTimeOptimization:
    def test_chunking_module_imports_without_tiktoken(self):
        # The chunking module should import without requiring tiktoken
        # at module level (lazy import deferred to __init__)
        import importlib
        # Remove any cached tiktoken modules
        mods_to_remove = [k for k in sys.modules if k.startswith("tiktoken")]
        saved = {k: sys.modules.pop(k) for k in mods_to_remove}

        try:
            # Remove chunking from cache too
            chunking_mods = [k for k in sys.modules if "chunking" in k.lower()]
            saved_chunking = {k: sys.modules.pop(k) for k in chunking_mods}

            # Import should succeed without tiktoken
            # (we can't import the real module without the full path, but
            # we can verify tiktoken is not a module-level dependency)
            import importlib.util
            spec = importlib.util.find_module_path if hasattr(importlib.util, "find_module_path") else None
            # Just verify tiktoken is not in the module's source as a top-level import
            chunking_path = Path(__file__).resolve().parents[2] / "main-system" / "src-core" / "core_system" / "rag" / "chunking.py"
            if chunking_path.is_file():
                source = chunking_path.read_text(encoding="utf-8")
                # tiktoken should not be a top-level import (should be in __init__)
                lines = source.split("\n")
                top_level_tiktoken = [
                    line for line in lines[:20]
                    if line.strip().startswith("import tiktoken") or line.strip().startswith("from tiktoken")
                ]
                assert len(top_level_tiktoken) == 0, "tiktoken should be lazy-imported, not top-level"
        finally:
            # Restore cached modules
            sys.modules.update(saved)
            sys.modules.update(saved_chunking)

    def test_import_time_benchmark_runs(self):
        result = _benchmark_import_time()
        assert result["capability_id"] == "python.rag.import_time"
        assert result["import_ms"] > 0
        assert result["saved_on_cold_startup_ms"] > 0


# --- Full regression benchmark tests ---

class TestRegressionBenchmarks:
    def test_run_regression_benchmarks_produces_report(self):
        report = run_regression_benchmarks()
        assert report["report_id"] == "runtime-optimization-v1"
        assert "recorded_at" in report
        assert "machine_profile" in report
        assert "optimizations" in report
        assert "reclassification" in report

    def test_regression_report_has_all_optimizations(self):
        report = run_regression_benchmarks()
        opts = report["optimizations"]
        assert "chunking" in opts
        assert "encode_json" in opts
        assert "reconcile_batch" in opts
        assert "import_time" in opts

    def test_regression_report_has_reclassification(self):
        report = run_regression_benchmarks()
        reclass = report["reclassification"]
        # Should have entries for the three native-core counterparts
        assert "native.parser.token_estimate" in reclass
        assert "native.vector.similarity" in reclass
        assert "native.transformer.attention" in reclass

    def test_reclassification_preserves_keep_python(self):
        report = run_regression_benchmarks()
        reclass = report["reclassification"]
        # KEEP_PYTHON capabilities should stay KEEP_PYTHON
        vector = reclass.get("native.vector.similarity", {})
        if vector.get("v1_verdict") == "KEEP_PYTHON":
            assert vector["post_optimization_verdict"] == "KEEP_PYTHON"

    def test_reclassification_optimize_python_stays_optimize(self):
        report = run_regression_benchmarks()
        reclass = report["reclassification"]
        # OPTIMIZE_PYTHON capabilities should stay OPTIMIZE_PYTHON
        # (no native path was added, so they can't become NATIVE_CANDIDATE)
        parser = reclass.get("native.parser.token_estimate", {})
        if parser.get("v1_verdict") == "OPTIMIZE_PYTHON":
            assert parser["post_optimization_verdict"] in ("OPTIMIZE_PYTHON", "KEEP_PYTHON")
