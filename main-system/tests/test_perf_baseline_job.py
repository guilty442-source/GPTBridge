"""§10.11 perf-baseline periodic collector + IPC latency ledger tests."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
sys.path.insert(0, str(SRC_CORE))

from ipc.latency_ledger import record, snapshot
from tasks.perf_baseline_job import build_tick, register


def test_latency_ledger_records_and_projects() -> None:
    for ms in (10.0, 20.0, 30.0, 40.0):
        record("unit-test-cmd", ms)
    snap = snapshot()
    assert snap["samples"] >= 4
    assert snap["p50_ms"] is not None
    assert snap["p95_ms"] >= snap["p50_ms"]
    assert snap["per_command"]["unit-test-cmd"]["samples"] >= 4


def test_register_uses_automation_core_when_present() -> None:
    calls: list[tuple] = []

    class Core:
        def register_flow(self, flow_id, tick, **kwargs):
            calls.append((flow_id, kwargs))
            return True

    app = SimpleNamespace(automation_core=Core())
    assert register(app, interval_s=300.0) is True
    assert calls == [("perf-baseline", {"interval_s": 300.0})]


def test_register_propagates_core_denial() -> None:
    class Core:
        def register_flow(self, flow_id, tick, **kwargs):
            return False

    scheduler = SimpleNamespace(register=lambda *a, **k: None)
    app = SimpleNamespace(automation_core=Core(), periodic_scheduler=scheduler)
    # 遭拒時不得改走 scheduler 私有迴圈（kill switch 語義）
    assert register(app) is False


def test_register_falls_back_to_scheduler() -> None:
    calls: list[tuple] = []

    class Scheduler:
        def register(self, name, interval_s, tick, *, pausable=False, **kw):
            calls.append((name, interval_s, pausable))

    app = SimpleNamespace(periodic_scheduler=Scheduler())
    assert register(app, interval_s=120.0) is True
    assert calls == [("perf-baseline", 120.0, True)]


def test_register_returns_false_without_governed_path() -> None:
    assert register(SimpleNamespace()) is False


@pytest.mark.asyncio
async def test_tick_collects_and_persists(tmp_path) -> None:
    app = SimpleNamespace(project_root=tmp_path)
    tick = build_tick(app)
    await tick()
    latest = (
        tmp_path / "main-system" / "runtime" / "state" / "perf-baseline-latest.json"
    )
    assert latest.exists()


def test_observe_stage_records_bounded_samples() -> None:
    """§10.11: per-stage latency surface for the perf baseline."""
    from core_system.rag.observability import RagMetrics

    m = RagMetrics(max_latency_samples=4)
    for ms in (10.0, 20.0, 30.0, 40.0, 50.0):
        m.observe_stage("reranker", ms)
    stage = m.snapshot()["stages"]["reranker"]
    assert stage["samples"] == 4  # bounded to max_latency_samples
    assert stage["p50_ms"] <= stage["p95_ms"]


def test_timed_stage_marks_trace_and_metrics() -> None:
    from core_system.rag.observability import (
        RagMetrics,
        begin_trace,
        current_trace,
        end_trace,
        timed_stage,
    )

    metrics = RagMetrics()
    begin_trace("rag-timed-1")
    try:
        with timed_stage("qdrant", metrics):
            pass
        trace = current_trace()
        assert trace is not None
        assert trace.timings.qdrant_ms >= 0
    finally:
        end_trace()
    assert metrics.snapshot()["stages"]["qdrant"]["samples"] == 1


def test_timed_stage_without_trace_still_records() -> None:
    from core_system.rag.observability import RagMetrics, timed_stage

    metrics = RagMetrics()
    with timed_stage("postgres_fts", metrics):
        pass
    assert metrics.snapshot()["stages"]["postgres_fts"]["samples"] == 1


def test_rag_metrics_none_until_runtime_started() -> None:
    """Lazy RAG (A586): never report metrics for a runtime that never started."""
    from tasks.perf_baseline_job import _rag_metrics

    assert _rag_metrics(SimpleNamespace()) is None
    assert _rag_metrics(SimpleNamespace(rag_runtime=None)) is None


def test_rag_metrics_snapshot_when_runtime_started() -> None:
    from tasks.perf_baseline_job import _rag_metrics

    snap = _rag_metrics(SimpleNamespace(rag_runtime=object()))
    assert snap is not None
    assert "stages" in snap
    assert "rag_query_total" in snap


@pytest.mark.asyncio
async def test_tick_injects_rag_snapshot(tmp_path) -> None:
    import json

    app = SimpleNamespace(project_root=tmp_path, rag_orchestrator=object())
    tick = build_tick(app)
    await tick()
    latest = (
        tmp_path / "main-system" / "runtime" / "state" / "perf-baseline-latest.json"
    )
    data = json.loads(latest.read_text(encoding="utf-8"))
    assert data["rag"] is not None
    assert "stages" in data["rag"]
