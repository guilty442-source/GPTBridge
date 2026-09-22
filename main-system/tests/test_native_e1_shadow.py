"""E1 native shadow bindings smoke tests (§10.65).

The pybind surface is verified when a fresh ``_sovereign_native`` is built;
until the running backend releases the locked .pyd the extension may be an
older build without the E1 classes — skip in that case.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
sys.path.insert(0, str(SRC_CORE))

try:
    from core_system.native import _sovereign_native as _native

    _E1 = all(
        hasattr(_native, name)
        for name in (
            "NativeWatchdog",
            "NativeScheduler",
            "NativeOutbox",
            "NativeMaintenance",
        )
    )
except ImportError:
    _E1 = False

requires_e1 = pytest.mark.skipif(
    not _E1, reason="native pyd predates E1 bindings (rebuild pending)"
)


@requires_e1
def test_watchdog_state_machine_and_repair_once():
    wd = _native.NativeWatchdog(1000, 60000, 3, 2)
    assert wd.state() == "unknown"
    ev = wd.probe(True, True, True, 1000)
    assert ev["to_state"] == "connected"
    assert wd.state() == "connected"
    # drive to disconnected: consecutive dead probes
    for i in range(6):
        wd.probe(False, False, False, 2000 + i * 1000)
    assert wd.state() == "disconnected"
    assert wd.consecutive_dead() >= 3
    assert wd.next_interval_ms() >= 1000


@requires_e1
def test_scheduler_fires_due_jobs_once():
    sched = _native.NativeScheduler()
    assert sched.register_job("heartbeat", 100, 50)
    assert sched.tick(50) == 0
    assert sched.tick(150) == 1
    stats = sched.job_stats("heartbeat")
    assert stats["run_count"] == 1
    assert stats["last_run_ms"] == 150


@requires_e1
def test_outbox_generation_mismatch_resets_cursor():
    ob = _native.NativeOutbox()
    assert ob.register_session("s1")
    res = ob.hello("s1", 5, False, 42)
    assert res["reset"] is True
    assert res["effective_cursor"] == 42
    # same-generation hello keeps cursor
    res2 = ob.hello("s1", 5, True, 42)
    assert res2["reset"] is False
    assert res2["effective_cursor"] == 5


@requires_e1
def test_outbox_ack_monotonic_and_drain_window():
    ob = _native.NativeOutbox()
    ob.register_session("s1")
    ob.hello("s1", 0, True, 0)
    assert ob.ack("s1", 10)
    assert not ob.ack("s1", 5)  # 單調不回退
    # drain 回傳投遞窗口（acked 之後、有界 batch）；已 ack 事件不重送
    plan = ob.drain_plan("s1", 1000, 2000)
    assert plan["has_work"] is True
    assert plan["start_after"] == 10
    assert plan["limit"] == 100
    # 窗口滿（sent_upto 達 acked+200）→ 背壓：無可送
    ob.mark_sent("s1", 210, 1500)
    plan2 = ob.drain_plan("s1", 1600, 2000)
    assert plan2["has_work"] is False


@requires_e1
def test_maintenance_admission_gate():
    mt = _native.NativeMaintenance(30000, 86400000, 3, 5000, 7)
    # M0 always admits; M1 needs idle; M2 needs authorized; M3 never runs
    assert mt.admit("m0", "a", 0, 1, 7, False, False, 1000)
    assert not mt.admit("m1", "a", 1, 1, 7, False, True, 1000)
    assert mt.admit("m1b", "a", 1, 1, 7, True, False, 1000)
    assert not mt.admit("m2", "a", 2, 1, 7, True, False, 1000)
    assert not mt.admit("m3", "a", 3, 1, 7, True, True, 1000)
    job = mt.next_due(2000)
    assert job["job_id"] == "m0"
    # generation mismatch must not dispatch
    mt2 = _native.NativeMaintenance(30000, 86400000, 3, 5000, 8)
    assert not mt2.admit("stale", "a", 0, 1, 7, True, True, 1000)
