"""P4 adaptive plane：observe_merge 多生產者欄位級合併。"""
from __future__ import annotations

import sys
from pathlib import Path

_SHARED = Path(__file__).resolve().parents[2] / "shared-layer" / "src"
_SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
for _p in (str(_SHARED), str(_SRC_CORE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared_layer.adaptive.plane import AdaptiveDataPlane  # noqa: E402
from shared_layer.adaptive.types import LoadSignals  # noqa: E402


def test_observe_merge_preserves_other_producers_fields():
    plane = AdaptiveDataPlane()
    # 生產者 A（maintenance controller）：pg／backlog 欄位
    plane.observe_merge(
        LoadSignals(pg_latency_ms=42.0, transport_backlog=7),
        fields=("pg_latency_ms", "transport_backlog"),
    )
    # 生產者 B（perf baseline）：cpu／ram 欄位——不得抹掉 A 的量測
    plane.observe_merge(
        LoadSignals(cpu_pct=55.0, ram_pct=61.0),
        fields=("cpu_pct", "ram_pct"),
    )
    signals = plane.signals
    assert signals.pg_latency_ms == 42.0
    assert signals.transport_backlog == 7
    assert signals.cpu_pct == 55.0
    assert signals.ram_pct == 61.0


def test_observe_merge_unknown_field_ignored():
    plane = AdaptiveDataPlane()
    plane.observe_merge(
        LoadSignals(cpu_pct=10.0),
        fields=("cpu_pct", "nonexistent_field"),
    )
    assert plane.signals.cpu_pct == 10.0
    assert not hasattr(plane.signals, "nonexistent_field")


def test_observe_merge_unlisted_field_not_written():
    """fields 之外的欄位即使帶值也不覆寫（欄位所有權）。"""
    plane = AdaptiveDataPlane()
    plane.observe(LoadSignals(pg_latency_ms=99.0, cpu_pct=20.0))
    plane.observe_merge(
        LoadSignals(cpu_pct=80.0, pg_latency_ms=999.0),
        fields=("cpu_pct",),
    )
    assert plane.signals.cpu_pct == 80.0
    assert plane.signals.pg_latency_ms == 99.0  # 未被覆寫


def test_perf_baseline_job_feeds_plane(monkeypatch):
    """perf-baseline tick 結尾把系統 cpu/ram 餵進 plane（merge 不覆寫他人）。"""
    import tasks.perf_baseline_job as job
    import core_system.model_resource_manager  # noqa: F401  (ensure src-core import path)

    from shared_layer.adaptive import get_plane
    import shared_layer.adaptive as adaptive_pkg

    plane = AdaptiveDataPlane()
    plane.observe_merge(
        LoadSignals(pg_latency_ms=33.0), fields=("pg_latency_ms",)
    )
    # patch the plane factory used inside _observe_adaptive_plane
    import shared_layer.adaptive
    monkeypatch.setattr(
        "shared_layer.adaptive.get_plane", lambda: plane
    )
    job._observe_adaptive_plane()
    assert plane.signals.pg_latency_ms == 33.0
    assert plane.signals.cpu_pct >= 0.0
    assert plane.signals.ram_pct >= 0.0
