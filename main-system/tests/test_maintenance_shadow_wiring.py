"""§10.65 act-1 shadow wiring tests for maintenance_controller.

The harness runs the C ``NativeMaintenance`` admission/queue model in
parallel while Python stays authoritative; these tests cover flag
parsing, admission-verdict parity, dispatch comparison, generation
re-init, and fail-closed disable behaviour.  When the governed extension
is not built the native-dependent cases skip.

Record payloads are asserted at ``kind``/``op`` level only — the
divergence record schema is owned by the shadow module and may evolve.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from tasks.maintenance_controller_native_shadow import (  # noqa: E402
    MaintenanceNativeShadow,
)
from shared_layer.database.maintenance import (  # noqa: E402
    scheduler as scheduler_mod,
)
from shared_layer.database.maintenance.evaluator import (  # noqa: E402
    MaintenanceCandidate,
)
from shared_layer.database.maintenance.models import (  # noqa: E402
    MaintenanceJobStatus,
    MaintenanceRiskClass,
)
from shared_layer.database.maintenance.scheduler import (  # noqa: E402
    MaintenanceScheduler,
)


def _write_policy(root: Path, mode: str) -> None:
    cfg = root / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text(
        json.dumps(
            {
                "schema": "native-shadow-policy/v1",
                "components": {"maintenance_controller": {"mode": mode}},
            }
        ),
        encoding="utf-8",
    )


def _native_available() -> bool:
    try:
        from core_system.native import _sovereign_native as native

        native.NativeMaintenance(30000, 3600000, 3, 60000, 0)
        return True
    except Exception:
        return False


requires_native = pytest.mark.skipif(
    not _native_available(),
    reason="native pyd unavailable or predates NativeMaintenance",
)


def _shadow(log: Path, native_cls, **kw) -> MaintenanceNativeShadow:
    args = dict(
        tick_interval_ms=30000,
        max_job_age_ms=3600000,
        max_retry_attempts=3,
        retry_backoff_ms=60000,
        current_generation=0,
        native_cls=native_cls,
    )
    args.update(kw)
    return MaintenanceNativeShadow(log, **args)


class _StubNative:
    """Controllable fake of ``NativeMaintenance``."""

    instances: list = []

    def __init__(self, tick_ms, age_ms, retries, backoff_ms, generation):
        self.ctor = (tick_ms, age_ms, retries, backoff_ms, generation)
        self.jobs = {}
        self.admit_verdict = True
        _StubNative.instances.append(self)

    def admit(
        self,
        job_id,
        action_id,
        risk_class,
        priority,
        generation,
        system_idle,
        authorized,
        now_ms,
        system_blocked=False,
    ):
        if not self.admit_verdict or system_blocked:
            return False
        self.jobs[job_id] = risk_class
        return True

    def next_due(self, now_ms):
        for job_id in self.jobs:
            return {
                "job_id": job_id,
                "action_id": "a",
                "status": 2,
                "attempt_count": 1,
            }
        return None

    def complete(self, job_id):
        return self.jobs.pop(job_id, None) is not None

    def fail(self, job_id, now_ms):
        return self.jobs.pop(job_id, None) is not None

    def cancel(self, job_id):
        return self.jobs.pop(job_id, None) is not None

    def job_count(self):
        return len(self.jobs)

    def cache_get(self, now_ms, ttl_ms):
        return self._cache_ok if getattr(self, "_cache_fresh", False) else None

    def cache_set(self, now_ms, ok):
        self._cache_fresh = True
        self._cache_ok = bool(ok)


class _RaisingNative:
    def __init__(self, *args, **kwargs):
        pass

    def admit(self, *args, **kwargs):
        raise RuntimeError("native boom")

    def next_due(self, *args, **kwargs):
        return None

    def complete(self, *args, **kwargs):
        return False

    def fail(self, *args, **kwargs):
        return False

    def job_count(self):
        return 0

    def cancel(self, *args, **kwargs):
        return False

    def cache_get(self, *args, **kwargs):
        return None

    def cache_set(self, *args, **kwargs):
        pass


def _records(path: Path) -> list:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


# --- policy gate ---


def test_from_policy_absent_without_file(tmp_path: Path) -> None:
    assert MaintenanceNativeShadow.from_policy(tmp_path) is None


def test_from_policy_absent_when_off(tmp_path: Path) -> None:
    _write_policy(tmp_path, "off")
    assert MaintenanceNativeShadow.from_policy(tmp_path) is None


def test_from_policy_refuses_primary(tmp_path: Path) -> None:
    _write_policy(tmp_path, "primary")
    assert MaintenanceNativeShadow.from_policy(tmp_path) is None


def test_from_policy_absent_on_invalid_json(tmp_path: Path) -> None:
    cfg = tmp_path / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text("{ nope", encoding="utf-8")
    assert MaintenanceNativeShadow.from_policy(tmp_path) is None


# --- admit parity ---


def test_admit_divergence_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative)
    shadow.observe_admit(
        "job-1",
        "act.vacuum",
        risk_class=MaintenanceRiskClass.M0_OBSERVE,
        priority=10,
        generation=0,
        system_idle=True,
        authorized=False,
        py_executable=False,
    )
    recs = _records(log)
    assert len(recs) == 1
    assert recs[0]["kind"] == "divergence"
    assert recs[0]["op"] == "admit"
    assert recs[0]["component"] == "maintenance_controller"


def test_admit_silent_match(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative)
    shadow.observe_admit(
        "job-1",
        "act.vacuum",
        risk_class="M0_OBSERVE",
        priority=10,
        generation=0,
        system_idle=True,
        authorized=False,
        py_executable=True,
    )
    assert not log.exists()


# --- dispatch parity ---


def test_dispatch_match_silent(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative)
    shadow._mt.jobs["j1"] = 0
    shadow.observe_dispatch("j1")
    assert not log.exists()


def test_dispatch_mismatch_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative)
    shadow._mt.jobs["j-native"] = 0
    shadow.observe_dispatch("j-python")
    recs = _records(log)
    assert [r["op"] for r in recs] == ["dispatch"]


def test_empty_dispatch_recorded_once(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative)
    shadow._mt.jobs["j-native"] = 0
    shadow.observe_dispatch(None)
    shadow.observe_dispatch(None)
    recs = _records(log)
    assert len(recs) == 1
    assert recs[0]["op"] == "dispatch"


# --- terminal mirror ---


def test_terminal_complete_miss_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative)
    shadow.observe_terminal("ghost", ok=True)
    recs = _records(log)
    assert [r["op"] for r in recs] == ["terminal"]


def test_terminal_fail_known_silent(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative)
    shadow._mt.jobs["j1"] = 0
    shadow.observe_terminal("j1", ok=False)
    assert not log.exists()


# --- fail-closed ---


def test_native_error_disables_once(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _RaisingNative)
    for _ in range(3):
        shadow.observe_admit(
            "j",
            "a",
            risk_class=0,
            priority=0,
            generation=0,
            system_idle=True,
            authorized=False,
            py_executable=True,
        )
    recs = _records(log)
    assert len(recs) == 1
    assert recs[0]["kind"] == "shadow-disabled"
    assert recs[0]["reason"] == "native-admit-error"


# --- generation re-init ---


def test_generation_change_rebuilds_native(tmp_path: Path) -> None:
    _StubNative.instances.clear()
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative, current_generation=1)
    shadow.observe_generation(1)
    assert len(_StubNative.instances) == 1
    shadow.observe_generation(2)
    assert len(_StubNative.instances) == 2
    assert _StubNative.instances[-1].ctor[4] == 2


# --- scheduler wiring ---


class _RecordingShadow:
    def __init__(self) -> None:
        self.calls = []

    def observe_generation(self, generation):
        self.calls.append(("generation", generation))

    def observe_admit(self, job_id, action_id, **kw):
        self.calls.append(("admit", job_id, action_id, kw))

    def observe_dispatch(self, py_job_id):
        self.calls.append(("dispatch", py_job_id))

    def observe_terminal(self, job_id, *, ok):
        self.calls.append(("terminal", job_id, ok))

    def observe_admit_veto(self, job_id):
        self.calls.append(("veto", job_id))


def _candidate(**kw) -> MaintenanceCandidate:
    base = dict(
        action_id="act.vacuum",
        engine="postgres",
        database_id="db1",
        risk_class=MaintenanceRiskClass.M0_OBSERVE,
        priority=5,
    )
    base.update(kw)
    return MaintenanceCandidate(**base)


def _patch_gates(monkeypatch, sched) -> None:
    action = SimpleNamespace(
        action_id="act.vacuum",
        lease_scope="db:{database_id}:vacuum",
        cooldown_seconds=0.0,
        timeout_seconds=60.0,
    )
    sched._registry = SimpleNamespace(get=lambda *a, **k: action)
    monkeypatch.setattr(
        scheduler_mod, "check_budget", lambda *a, **k: (True, "")
    )
    monkeypatch.setattr(
        scheduler_mod, "check_lease_conflict", lambda *a, **k: (False, None)
    )
    monkeypatch.setattr(
        scheduler_mod,
        "acquire_lease",
        lambda *a, **k: SimpleNamespace(lease_until=None),
    )
    sched.budget.check_cooldown = lambda *a, **k: (True, "")


def _allow_all(risk_class, system_state):
    return SimpleNamespace(allowed=True)


def test_scheduler_hooks_reach_shadow(monkeypatch) -> None:
    sched = MaintenanceScheduler(policy_evaluator=_allow_all)
    rec = _RecordingShadow()
    sched.set_native_shadow(rec)

    sched.set_generation(4)
    assert ("generation", 4) in rec.calls

    _patch_gates(monkeypatch, sched)
    cand = _candidate()
    job = sched._try_admit(cand, signals={}, context={})
    assert job is not None
    admit_calls = [c for c in rec.calls if c[0] == "admit"]
    assert len(admit_calls) == 1
    assert admit_calls[0][1] == str(cand.candidate_id)
    assert admit_calls[0][3]["py_executable"] is True

    pair = sched.get_next_job()
    assert pair is not None
    disp = [c for c in rec.calls if c[0] == "dispatch"]
    assert disp == [("dispatch", str(job.job_id))]

    ok = sched.complete_job(job.job_id, MaintenanceJobStatus.SUCCEEDED)
    assert ok is True
    assert ("terminal", str(job.job_id), True) in rec.calls


def test_scheduler_m3_observed_not_executable(monkeypatch) -> None:
    sched = MaintenanceScheduler(
        policy_evaluator=lambda risk_class, system_state: SimpleNamespace(
            allowed=False
        )
    )
    rec = _RecordingShadow()
    sched.set_native_shadow(rec)
    _patch_gates(monkeypatch, sched)
    cand = _candidate(risk_class=MaintenanceRiskClass.M3_APPROVAL_REQUIRED)
    job = sched._try_admit(cand, signals={}, context={})
    assert job is not None
    assert job.status == MaintenanceJobStatus.PLANNED
    admit_calls = [c for c in rec.calls if c[0] == "admit"]
    assert admit_calls[0][3]["py_executable"] is False


def test_scheduler_empty_dispatch_observed() -> None:
    sched = MaintenanceScheduler(policy_evaluator=_allow_all)
    rec = _RecordingShadow()
    sched.set_native_shadow(rec)
    assert sched.get_next_job() is None
    assert ("dispatch", None) in rec.calls


def test_scheduler_works_without_shadow() -> None:
    sched = MaintenanceScheduler(policy_evaluator=_allow_all)
    assert sched._native_shadow is None
    assert sched.get_next_job() is None


def test_scheduler_downstream_veto_reaches_shadow(monkeypatch) -> None:
    """Budget refusal after a policy-allowed admit must veto the mirror."""
    sched = MaintenanceScheduler(policy_evaluator=_allow_all)
    rec = _RecordingShadow()
    sched.set_native_shadow(rec)
    _patch_gates(monkeypatch, sched)
    monkeypatch.setattr(
        scheduler_mod, "check_budget", lambda *a, **k: (False, "over")
    )
    cand = _candidate()
    job = sched._try_admit(cand, signals={}, context={})
    assert job is None
    assert ("veto", str(cand.candidate_id)) in rec.calls


def test_admit_system_blocked_refuses(tmp_path: Path) -> None:
    """Global block (recovery/drain/cooldown/lease) refuses even M0."""
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative)
    shadow.observe_admit(
        "job-b",
        "act.vacuum",
        risk_class=MaintenanceRiskClass.M0_OBSERVE,
        priority=10,
        generation=0,
        system_idle=False,
        authorized=False,
        py_executable=False,
        policy_context={"recovery_state": "RECOVERING"},
    )
    # stub refuses on system_blocked -> matches py_executable=False -> silent
    assert not log.exists()
    assert "job-b" not in shadow._mt.jobs


def test_admit_veto_cancels_native_job(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative)
    shadow._mt.jobs["j1"] = 0
    shadow.observe_admit_veto("j1")
    assert "j1" not in shadow._mt.jobs
    assert not log.exists()


def test_probe_cache_parity(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = _shadow(log, _StubNative)
    # both miss initially -> silent
    shadow.observe_probe_cache(now_s=1.0, ttl_s=20.0, py_hit=False)
    assert not log.exists()
    shadow.observe_probe_store(now_s=1.0, probe_ok=True)
    shadow.observe_probe_cache(now_s=2.0, ttl_s=20.0, py_hit=True)
    assert not log.exists()
    # python hit but native missed -> divergence
    shadow._mt._cache_fresh = False
    shadow.observe_probe_cache(now_s=3.0, ttl_s=20.0, py_hit=True)
    recs = _records(log)
    assert len(recs) == 1 and recs[0]["op"] == "probe_cache"


@requires_native
def test_real_native_lockstep_m0_m3(tmp_path: Path) -> None:
    _write_policy(tmp_path, "shadow")
    shadow = MaintenanceNativeShadow.from_policy(tmp_path)
    assert shadow is not None
    shadow.observe_admit(
        "j0",
        "act",
        risk_class=MaintenanceRiskClass.M0_OBSERVE,
        priority=0,
        generation=0,
        system_idle=True,
        authorized=False,
        py_executable=True,
    )
    shadow.observe_admit(
        "j3",
        "act",
        risk_class=MaintenanceRiskClass.M3_APPROVAL_REQUIRED,
        priority=0,
        generation=0,
        system_idle=True,
        authorized=True,
        py_executable=False,
    )
    log = (
        tmp_path
        / "main-system"
        / "runtime"
        / "logs"
        / "native-shadow"
        / "maintenance-controller.jsonl"
    )
    assert _records(log) == []
