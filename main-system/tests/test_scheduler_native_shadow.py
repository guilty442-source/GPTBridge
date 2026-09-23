"""Blueprint 10.65 act-1 shadow wiring tests for periodic_scheduler.

The harness runs the C ``NativeScheduler`` job table in parallel while
Python stays authoritative; these tests cover flag parsing, parity
silence, divergence auditing, refusal accounting, and fail-closed
behaviour.  When the governed extension is not built the
native-dependent cases degrade to flag/paths checks only.

Record payloads are asserted at ``kind`` level only — the divergence
record schema is owned by the shadow module and may evolve.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from tasks.periodic_scheduler import PeriodicScheduler
from tasks.periodic_scheduler_native_shadow import SchedulerNativeShadow


def _write_policy(root: Path, mode: str) -> None:
    cfg = root / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text(
        json.dumps(
            {
                "schema": "native-shadow-policy/v1",
                "components": {"periodic_scheduler": {"mode": mode}},
            }
        ),
        encoding="utf-8",
    )


def _native_sched() -> object:
    from core_system.native import _sovereign_native as native

    return native.NativeScheduler()


def _native_available() -> bool:
    try:
        _native_sched()
    except Exception:
        return False
    return True


requires_native = pytest.mark.skipif(
    not _native_available(), reason="native pyd unavailable or predates E1 bindings"
)


def test_shadow_absent_without_policy(tmp_path: Path) -> None:
    sched = PeriodicScheduler(project_root=tmp_path)
    assert sched._native_shadow is None


def test_shadow_absent_when_mode_off(tmp_path: Path) -> None:
    _write_policy(tmp_path, "off")
    sched = PeriodicScheduler(project_root=tmp_path)
    assert sched._native_shadow is None


def test_primary_mode_refused_fail_closed(tmp_path: Path) -> None:
    # act-2 mode is not wired in act-1: refuse -> Python-only
    _write_policy(tmp_path, "primary")
    sched = PeriodicScheduler(project_root=tmp_path)
    assert sched._native_shadow is None


def test_shadow_absent_on_invalid_policy(tmp_path: Path) -> None:
    cfg = tmp_path / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text("{ not json", encoding="utf-8")
    sched = PeriodicScheduler(project_root=tmp_path)
    assert sched._native_shadow is None


class _StubNative:
    """Fake native scheduler returning divergent per-job stats."""

    def __init__(self) -> None:
        self.jobs: dict[str, dict] = {}
        self.tick_calls = 0

    def register_job(self, name, interval_ms, timeout_ms, now_ms, run_immediately, pausable):
        self.jobs[name] = {
            "run_count": 99,
            "paused_count": 7,
            "next_due_ms": now_ms + interval_ms,
        }
        return True

    def unregister_job(self, name):
        self.jobs.pop(name, None)
        return True

    def tick(self, now_ms, paused):
        self.tick_calls += 1
        return 0

    def job_count(self):
        return len(self.jobs)

    def job_stats(self, name):
        return self.jobs.get(name)


class _RaisingNative:
    def register_job(self, *args, **kwargs):
        raise RuntimeError("native boom")


def _py_job(next_due: float, run_count: int = 0, paused_count: int = 0) -> dict:
    return {
        "interval_s": 60.0,
        "tick": None,
        "pausable": False,
        "timeout_s": 120.0,
        "next_due": next_due,
        "run_count": run_count,
        "paused_count": paused_count,
    }


def test_divergence_recorded_python_authoritative(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = SchedulerNativeShadow(log, _StubNative())
    now = time.monotonic()
    shadow.observe_register(
        "job", interval_s=60.0, timeout_s=120.0,
        run_immediately=False, pausable=False, now_s=now,
    )
    shadow.observe_tick(
        now_s=now, paused=False,
        py_jobs={"job": _py_job(now + 60.0, run_count=0)},
    )
    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    rec = records[0]
    assert rec["kind"] == "divergence"
    assert rec["component"] == "periodic_scheduler"
    assert "run_count" in rec["mismatches"]
    assert "paused_count" in rec["mismatches"]


def test_membership_divergence_when_native_missing(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = SchedulerNativeShadow(log, _StubNative())
    now = time.monotonic()
    shadow.observe_tick(
        now_s=now, paused=False,
        py_jobs={"ghost": _py_job(now + 60.0)},
    )
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["kind"] == "membership-divergence"


def test_register_refused_recorded(tmp_path: Path) -> None:
    class _FullNative(_StubNative):
        def register_job(self, name, *args, **kwargs):
            return False

    log = tmp_path / "x.jsonl"
    shadow = SchedulerNativeShadow(log, _FullNative())
    now = time.monotonic()
    shadow.observe_register(
        "a", interval_s=1.0, timeout_s=1.0,
        run_immediately=False, pausable=False, now_s=now,
    )
    # refused job stays python-side only -> next tick reports membership gap
    shadow.observe_tick(
        now_s=now, paused=False,
        py_jobs={"a": _py_job(now)},
    )
    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    assert [r["kind"] for r in records] == [
        "register-refused",
        "membership-divergence",
    ]


def test_native_error_fail_closed_single_record(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = SchedulerNativeShadow(log, _RaisingNative())
    for _ in range(3):
        shadow.observe_register(
            "job", interval_s=1.0, timeout_s=1.0,
            run_immediately=False, pausable=False, now_s=time.monotonic(),
        )
    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["kind"] == "shadow-disabled"
    assert records[0]["reason"] == "native-register-error"


def test_silent_match_writes_nothing(tmp_path: Path) -> None:
    class _MatchingNative(_StubNative):
        def register_job(self, name, interval_ms, timeout_ms, now_ms, run_immediately, pausable):
            self.jobs[name] = {
                "run_count": 0,
                "paused_count": 0,
                "next_due_ms": now_ms + interval_ms,
            }
            return True

    log = tmp_path / "x.jsonl"
    shadow = SchedulerNativeShadow(log, _MatchingNative())
    now = time.monotonic()
    shadow.observe_register(
        "job", interval_s=60.0, timeout_s=120.0,
        run_immediately=False, pausable=False, now_s=now,
    )
    shadow.observe_tick(
        now_s=now, paused=False,
        py_jobs={"job": _py_job(now + 60.0)},
    )
    assert not log.exists()


def _read_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _shadow_log(tmp_path: Path) -> Path:
    return (
        tmp_path
        / "main-system"
        / "runtime"
        / "logs"
        / "native-shadow"
        / "periodic-scheduler.jsonl"
    )


async def _wait_for(predicate, timeout_s: float = 15.0) -> bool:
    """Poll until predicate() — loaded hosts delay asyncio timers far past
    their nominal deadline, so fixed sleeps are unreliable evidence."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def _bad_records(records: list[dict]) -> list[dict]:
    return [
        r for r in records
        if r["kind"] in ("divergence", "membership-divergence", "register-refused")
    ]


@requires_native
@pytest.mark.asyncio
async def test_real_native_shadow_stays_in_lockstep(tmp_path: Path) -> None:
    _write_policy(tmp_path, "shadow")
    sched = PeriodicScheduler(tick_seconds=0.02, project_root=tmp_path)
    assert sched._native_shadow is not None
    ran: list[str] = []

    async def job() -> None:
        ran.append("x")

    sched.register("a", 0.05, job, run_immediately=True)
    sched.register("b", 0.05, job, pausable=True)
    observed = await _wait_for(
        lambda: all(
            j["run_count"] >= 1 for j in sched.jobs()
        )
        and len(sched.jobs()) == 2
    )
    await sched.stop()
    assert observed, "jobs never ran within polling window"
    assert _bad_records(_read_records(_shadow_log(tmp_path))) == []


@requires_native
@pytest.mark.asyncio
async def test_real_native_pause_defers_pausable_in_lockstep(
    tmp_path: Path,
) -> None:
    _write_policy(tmp_path, "shadow")
    sched = PeriodicScheduler(
        tick_seconds=0.02, pause_check=lambda: True, project_root=tmp_path
    )
    ran: list[str] = []

    async def job() -> None:
        ran.append("x")

    sched.register("defer", 0.05, job, pausable=True)
    sched.register("essential", 0.05, job, pausable=False)
    observed = await _wait_for(
        lambda: (
            lambda jobs: jobs.get("essential", {}).get("run_count", 0) >= 1
            and jobs.get("defer", {}).get("paused_count", 0) >= 1
        )({j["name"]: j for j in sched.jobs()})
    )
    await sched.stop()
    assert observed, "pause/defer behaviour never observed within window"
    assert _bad_records(_read_records(_shadow_log(tmp_path))) == []
    jobs = {j["name"]: j for j in sched.jobs()}
    assert jobs["defer"]["run_count"] == 0
