"""§10.63 R3 — shared PeriodicScheduler consolidation tests."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
sys.path.insert(0, str(SRC_CORE))

from core_system.daily_global_cleaner_service import DailyGlobalCleanerService
from core_system.resource_maintenance import IdleMemoryMaintainer
from core_system.system_automation_coordinator import SystemAutomationCoordinator
from tasks.periodic_scheduler import PeriodicScheduler


@pytest.mark.asyncio
async def test_registered_job_runs_at_deadline() -> None:
    scheduler = PeriodicScheduler(tick_seconds=0.02)
    ran: list[float] = []

    async def job() -> str:
        ran.append(time.monotonic())
        return "ok"

    scheduler.register("job", 0.05, job)
    await asyncio.sleep(0.2)
    await scheduler.stop()
    assert ran, "registered job never ran"


@pytest.mark.asyncio
async def test_job_failure_isolated_and_recorded() -> None:
    scheduler = PeriodicScheduler(tick_seconds=0.02)
    good_runs: list[str] = []

    async def bad() -> None:
        raise RuntimeError("boom")

    async def good() -> str:
        good_runs.append("x")
        return "ok"

    scheduler.register("bad", 0.02, bad, run_immediately=True)
    scheduler.register("good", 0.02, good, run_immediately=True)
    await asyncio.sleep(0.15)
    jobs = {j["name"]: j for j in scheduler.jobs()}
    await scheduler.stop()
    assert good_runs, "good job starved by failing sibling"
    assert jobs["bad"]["last_error"].startswith("RuntimeError")
    assert jobs["good"]["last_error"] is None
    assert jobs["good"]["run_count"] >= 1


@pytest.mark.asyncio
async def test_job_timeout_enforced() -> None:
    scheduler = PeriodicScheduler(tick_seconds=0.02)

    async def hang() -> None:
        await asyncio.sleep(30)

    scheduler.register("hang", 0.02, hang, run_immediately=True, timeout_s=0.05)
    await asyncio.sleep(0.2)
    jobs = {j["name"]: j for j in scheduler.jobs()}
    await scheduler.stop()
    assert "TimeoutError" in jobs["hang"]["last_error"]


@pytest.mark.asyncio
async def test_unregister_removes_job() -> None:
    scheduler = PeriodicScheduler(tick_seconds=0.02)
    counter = {"n": 0}

    async def job() -> int:
        counter["n"] += 1
        return counter["n"]

    scheduler.register("job", 0.02, job, run_immediately=True)
    await asyncio.sleep(0.1)
    scheduler.unregister("job")
    snapshot = counter["n"]
    await asyncio.sleep(0.1)
    await scheduler.stop()
    assert snapshot >= 1
    assert counter["n"] == snapshot, "unregistered job still ran"


@pytest.mark.asyncio
async def test_start_stop_idempotent() -> None:
    scheduler = PeriodicScheduler()
    assert scheduler.start()["status"] == "started"
    assert scheduler.start()["status"] == "already_running"
    await scheduler.stop()
    await scheduler.stop()


@pytest.mark.asyncio
async def test_coordinator_rides_scheduler_without_own_loop() -> None:
    class _Perm:
        def tool_execution_response(self, tool_id: str, request_id: str):
            return None

    app = SimpleNamespace(
        periodic_scheduler=PeriodicScheduler(),
        permission_sovereign=_Perm(),
        decision_sovereign=None,
        xingcheng_sovereign=None,
        maintenance_sovereign=None,
        update_sovereign=None,
    )
    coordinator = SystemAutomationCoordinator(app)
    await coordinator.start()
    assert coordinator._task is None, "coordinator spawned a private loop"
    assert any(
        j["name"] == "system-automation-coordinator"
        for j in app.periodic_scheduler.jobs()
    )
    await coordinator.stop()
    await app.periodic_scheduler.stop()
    assert not any(
        j["name"] == "system-automation-coordinator"
        for j in app.periodic_scheduler.jobs()
    )


@pytest.mark.asyncio
async def test_coordinator_scheduled_tick_obeys_adaptive_due() -> None:
    class _Perm:
        def tool_execution_response(self, tool_id: str, request_id: str):
            return None

    app = SimpleNamespace(
        periodic_scheduler=PeriodicScheduler(),
        permission_sovereign=_Perm(),
        decision_sovereign=None,
        xingcheng_sovereign=None,
        maintenance_sovereign=None,
        update_sovereign=None,
    )
    coordinator = SystemAutomationCoordinator(app)
    coordinator._running = True
    coordinator._last_cycle_monotonic = time.monotonic()
    ran = {"n": 0}

    async def _cycle() -> None:
        ran["n"] += 1

    coordinator._coordination_cycle = _cycle  # type: ignore[method-assign]
    await coordinator._scheduled_tick()
    assert ran["n"] == 0, "tick ran before the adaptive interval elapsed"
    coordinator._last_cycle_monotonic = (
        time.monotonic() - coordinator._adaptive_interval - 1
    )
    await coordinator._scheduled_tick()
    assert ran["n"] == 1, "due tick did not run the cycle"


@pytest.mark.asyncio
async def test_cleaner_scheduled_tick_respects_due_gate() -> None:
    cleaner = DailyGlobalCleanerService.__new__(DailyGlobalCleanerService)
    cleaner._stop_event = asyncio.Event()
    calls = {"due": 0, "run": 0}
    cleaner.is_due = lambda now=None: calls.__setitem__("due", calls["due"] + 1) or False  # type: ignore[method-assign]

    async def _run_if_due() -> None:
        calls["run"] += 1

    cleaner.run_if_due = _run_if_due  # type: ignore[method-assign]
    await cleaner._scheduled_tick()
    assert calls["due"] == 1 and calls["run"] == 0


@pytest.mark.asyncio
async def test_git_automation_rides_scheduler(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    from tasks.git_automation import GitAutomationService

    scheduler = PeriodicScheduler()
    service = GitAutomationService(tmp_path, scheduler=scheduler)
    result = await service.start()
    assert result["loop"] == "periodic-scheduler"
    assert service._task is None, "git automation spawned a private loop"
    assert any(
        j["name"] == "git-automation" for j in scheduler.jobs()
    )
    await service.stop()
    assert not any(
        j["name"] == "git-automation" for j in scheduler.jobs()
    )
    await scheduler.stop()


@pytest.mark.asyncio
async def test_maintainer_tick_respects_idle_gate() -> None:
    maintainer = IdleMemoryMaintainer(
        is_busy=lambda: True, interval_seconds=60, minimum_idle_seconds=30
    )
    await maintainer.tick()
    assert maintainer.last_result is None, "busy maintainer released memory"

    idle = IdleMemoryMaintainer(
        is_busy=lambda: False, interval_seconds=60, minimum_idle_seconds=30
    )
    idle.last_activity = 0.0
    idle.release = lambda: {"released": True}  # type: ignore[method-assign]
    await idle.tick()
    assert idle.last_result == {"released": True}


@pytest.mark.asyncio
async def test_pausable_jobs_defer_under_regulation() -> None:
    """§10.64 ④: pausable jobs defer while the governor regulates;
    essential jobs keep running; due times shift (no burst on release)."""
    paused = {"on": True}
    scheduler = PeriodicScheduler(
        tick_seconds=0.02, pause_check=lambda: paused["on"]
    )
    calls: list[str] = []

    async def _pausable() -> None:
        calls.append("pausable")

    async def _essential() -> None:
        calls.append("essential")

    scheduler.register("pausable-job", 0.02, _pausable, pausable=True)
    scheduler.register("essential-job", 0.02, _essential)
    await asyncio.sleep(0.12)
    paused["on"] = False
    await asyncio.sleep(0.06)
    await scheduler.stop()

    job = {j["name"]: j for j in scheduler.jobs()}
    assert job["pausable-job"]["paused_count"] >= 2
    assert job["essential-job"]["run_count"] >= 2
    assert calls.count("pausable") >= 1, "job must run after release"
    assert job["pausable-job"]["run_count"] == calls.count("pausable")


@pytest.mark.asyncio
async def test_no_pause_check_never_defers() -> None:
    scheduler = PeriodicScheduler(tick_seconds=0.02)
    calls: list[str] = []

    async def _tick() -> None:
        calls.append("tick")

    scheduler.register("job", 0.02, _tick, pausable=True)
    await asyncio.sleep(0.07)
    await scheduler.stop()
    assert calls, "pausable job must run when no pause_check is set"


def test_governor_signal_fails_open(tmp_path, monkeypatch) -> None:
    from tasks import resource_governor_signal as sig

    monkeypatch.setattr(
        sig, "_STATE_FILE", tmp_path / "resource-governor.json"
    )
    assert sig.regulation_active() is False
    assert sig.worker_admission_hold() is False

    sig._STATE_FILE.write_text(
        '{"regulation": {"active": true}, "worker_admission_hold": true}',
        encoding="utf-8",
    )
    assert sig.regulation_active() is True
    assert sig.worker_admission_hold() is True

    sig._STATE_FILE.write_text("{broken json", encoding="utf-8")
    assert sig.regulation_active() is False
    assert sig.worker_admission_hold() is False
