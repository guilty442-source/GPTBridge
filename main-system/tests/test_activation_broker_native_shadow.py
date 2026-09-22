"""section 10.65 act-1 shadow wiring tests for model_service_activation.

The harness runs the C NativeActivationBroker decision ladder in
parallel while Python stays authoritative; covers flag parsing,
ensure/start/release/explicit-stop parity, status counter compare,
poll-interval and write-due mirrors, divergence evidence and
fail-closed disable.  Native-dependent cases skip when the governed
extension is not built.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from tasks.model_service_activation import (  # noqa: E402
    ModelServiceActivationBroker,
)
from tasks.model_service_activation_native_shadow import (  # noqa: E402
    ActivationBrokerNativeShadow,
)


def _write_policy(root: Path, mode: str) -> None:
    cfg = root / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text(
        json.dumps(
            {
                "schema": "native-shadow-policy/v1",
                "components": {"model_service_activation": {"mode": mode}},
            }
        ),
        encoding="utf-8",
    )


def _native_available() -> bool:
    try:
        from core_system.native import _sovereign_native as native

        native.NativeActivationBroker(20.0, 15.0, 180.0)
        return True
    except Exception:
        return False


requires_native = pytest.mark.skipif(
    not _native_available(),
    reason="native pyd unavailable or predates NativeActivationBroker",
)


def _records(path: Path) -> list:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def _inputs(**over):
    base = {
        "pending": True,
        "maintenance_ready": True,
        "shutting_down": False,
        "admission_hold": False,
        "liveness_known": True,
        "owner_active": False,
        "regulation_active": False,
        "now_monotonic": 100.0,
    }
    base.update(over)
    return base


class _StubBroker:
    """Controllable fake of NativeActivationBroker."""

    def __init__(self, ensure_decision="should-start") -> None:
        self.ensure_decision = ensure_decision
        self.start_decision = "started"
        self.release_decision = "released"
        self.status_payload = {
            "attempts": 1,
            "backoff_seconds": 15.0,
            "next_attempt_at": 120.0,
            "next_release_at": 0.0,
            "broker_started_owner": True,
            "explicit_stop_at": 0.0,
        }
        self.ensure_calls = []
        self.stops = []

    def ensure(self, inputs):
        self.ensure_calls.append(inputs)
        return self.ensure_decision

    def on_start_result(self, ok, now):
        return self.start_decision if ok else "start-failed"

    def on_release_result(self, ok, now):
        return self.release_decision if ok else "release-failed"

    def note_explicit_stop(self, now, wall):
        self.stops.append((now, wall))

    def status(self):
        return dict(self.status_payload)

    @staticmethod
    def poll_interval(pending, idle_s, pending_s):
        return pending_s if pending else idle_s

    @staticmethod
    def state_write_due(changed, now, last, heartbeat):
        return bool(changed) or (now - last) >= heartbeat


class _RaisingBroker:
    def __getattr__(self, name):
        raise RuntimeError("native boom")


# --- policy gate ---


def test_from_policy_absent_without_file(tmp_path: Path) -> None:
    assert ActivationBrokerNativeShadow.from_policy(tmp_path) is None


def test_from_policy_absent_when_off(tmp_path: Path) -> None:
    _write_policy(tmp_path, "off")
    assert ActivationBrokerNativeShadow.from_policy(tmp_path) is None


def test_from_policy_refuses_primary(tmp_path: Path) -> None:
    _write_policy(tmp_path, "primary")
    assert ActivationBrokerNativeShadow.from_policy(tmp_path) is None


def test_from_policy_absent_on_invalid_json(tmp_path: Path) -> None:
    cfg = tmp_path / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text("{ nope", encoding="utf-8")
    assert ActivationBrokerNativeShadow.from_policy(tmp_path) is None


# --- decision parity ---


def test_ensure_parity_silent(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = ActivationBrokerNativeShadow(log, _StubBroker("should-start"))
    shadow.observe_ensure(_inputs(), py_decision="should-start")
    assert not log.exists()


def test_ensure_divergence_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = ActivationBrokerNativeShadow(log, _StubBroker("throttled"))
    shadow.observe_ensure(_inputs(), py_decision="should-start")
    recs = _records(log)
    assert [r["op"] for r in recs] == ["ensure"]
    assert recs[0]["native"]["decision"] == "throttled"


def test_start_result_divergence(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubBroker()
    stub.start_decision = "start-failed"
    shadow = ActivationBrokerNativeShadow(log, stub)
    shadow.observe_start_result(True, 100.0, py_decision="started")
    recs = _records(log)
    assert [r["op"] for r in recs] == ["start_result"]


def test_release_result_parity(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = ActivationBrokerNativeShadow(log, _StubBroker())
    shadow.observe_release_result(True, 100.0, py_decision="released")
    assert not log.exists()


def test_explicit_stop_mirrored(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubBroker()
    shadow = ActivationBrokerNativeShadow(log, stub)
    shadow.observe_explicit_stop(50.0, 9999.0)
    assert stub.stops == [(50.0, 9999.0)]
    assert not log.exists()


def test_status_mismatch_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubBroker()
    stub.status_payload["attempts"] = 7
    shadow = ActivationBrokerNativeShadow(log, stub)
    shadow.observe_status(
        {
            "attempts": 1,
            "backoff_seconds": 15.0,
            "next_attempt_at": 120.0,
            "next_release_at": 0.0,
            "broker_started_owner": True,
            "explicit_stop_at": 0.0,
        }
    )
    recs = _records(log)
    assert [r["op"] for r in recs] == ["status"]
    assert "attempts" in recs[0]["mismatches"]


def test_poll_interval_mismatch(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = ActivationBrokerNativeShadow(log, _StubBroker())
    shadow.observe_poll_interval(False, 5.0, 1.0, py_interval=1.0)
    recs = _records(log)
    assert [r["op"] for r in recs] == ["poll_interval"]


def test_write_due_mismatch(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = ActivationBrokerNativeShadow(log, _StubBroker())
    shadow.observe_state_write_due(False, 30.0, 0.0, 60.0, py_due=True)
    recs = _records(log)
    assert [r["op"] for r in recs] == ["state_write_due"]


# --- fail-closed ---


def test_native_error_disables_once(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = ActivationBrokerNativeShadow(log, _RaisingBroker())
    for _ in range(3):
        shadow.observe_ensure(_inputs(), py_decision="should-start")
    recs = _records(log)
    assert len(recs) == 1
    assert recs[0]["kind"] == "shadow-disabled"


# --- broker wiring ---


class _RecordingShadow:
    def __init__(self) -> None:
        self.calls = []

    def observe_ensure(self, inputs, *, py_decision):
        self.calls.append(("ensure", py_decision, dict(inputs)))

    def observe_start_result(self, ok, now, *, py_decision):
        self.calls.append(("start", ok, py_decision))

    def observe_release_result(self, ok, now, *, py_decision):
        self.calls.append(("release", ok, py_decision))

    def observe_explicit_stop(self, now, wall):
        self.calls.append(("stop", now, wall))

    def observe_status(self, py_state):
        self.calls.append(("status", dict(py_state)))

    def observe_poll_interval(self, pending, idle_s, pending_s, *, py_interval):
        pass

    def observe_state_write_due(self, *a, **k):
        pass


class _FakeToolbox:
    def __init__(self, active=False, start_ok=True):
        self.active = active
        self.start_ok = start_ok

    async def tool_process_active(self, tool_id):
        return self.active

    async def start_tool(self, payload):
        return {"ok": self.start_ok, "pid": 4321}

    async def stop_tool(self, payload):
        return {"ok": True}


def _broker(toolbox, **kwargs):
    app = SimpleNamespace(maintenance_ready=True, _shutting_down=False)
    kwargs.setdefault("idle_interval", 5.0)
    kwargs.setdefault("pending_interval", 1.0)
    return ModelServiceActivationBroker(app, toolbox, **kwargs)


def test_broker_no_shadow_when_policy_off(tmp_path: Path) -> None:
    broker = _broker(_FakeToolbox(), project_root=tmp_path)
    assert broker._native_shadow is None


def test_broker_decision_reaches_shadow(tmp_path: Path) -> None:
    broker = _broker(_FakeToolbox(active=True))
    rec = _RecordingShadow()
    broker._native_shadow = rec
    with patch.object(broker, "_has_pending_dialogue_request", return_value=True):
        decision = asyncio.run(broker._ensure_inner())
    assert decision == "owner-running"
    assert ("ensure", "owner-running", pytest.approx(0, abs=1)) or True
    ensure_calls = [c for c in rec.calls if c[0] == "ensure"]
    assert ensure_calls and ensure_calls[0][1] == "owner-running"
    assert ensure_calls[0][2]["owner_active"] is True


def test_broker_start_path_mirrored(tmp_path: Path) -> None:
    broker = _broker(_FakeToolbox(active=False, start_ok=True))
    rec = _RecordingShadow()
    broker._native_shadow = rec
    with patch.object(broker, "_has_pending_dialogue_request", return_value=True):
        decision = asyncio.run(broker._ensure_inner())
    assert decision == "started"
    kinds = [c[0] for c in rec.calls]
    assert "ensure" in kinds and "start" in kinds
    assert ("start", True, "started") in rec.calls


def test_broker_explicit_stop_mirrored(tmp_path: Path) -> None:
    broker = _broker(_FakeToolbox())
    rec = _RecordingShadow()
    broker._native_shadow = rec
    broker.note_explicit_owner_stop()
    stops = [c for c in rec.calls if c[0] == "stop"]
    assert stops and stops[0][2] == broker._explicit_stop_at


@requires_native
def test_real_native_lockstep_started(tmp_path: Path) -> None:
    _write_policy(tmp_path, "shadow")
    broker = _broker(
        _FakeToolbox(active=False, start_ok=True), project_root=tmp_path
    )
    assert broker._native_shadow is not None
    with patch.object(broker, "_has_pending_dialogue_request", return_value=True):
        assert asyncio.run(broker._ensure_inner()) == "started"
    broker._native_shadow.observe_status(
        {
            "attempts": broker._attempts,
            "backoff_seconds": broker._backoff,
            "next_attempt_at": broker._next_attempt_at,
            "next_release_at": broker._next_release_at,
            "broker_started_owner": broker._broker_started_owner,
            "explicit_stop_at": broker._explicit_stop_at,
        }
    )
    log = (
        tmp_path
        / "main-system"
        / "runtime"
        / "logs"
        / "native-shadow"
        / "model-service-activation.jsonl"
    )
    assert _records(log) == []


@requires_native
def test_real_native_lockstep_release(tmp_path: Path) -> None:
    _write_policy(tmp_path, "shadow")
    toolbox = _FakeToolbox(active=False, start_ok=True)
    broker = _broker(toolbox, project_root=tmp_path)
    with patch.object(broker, "_has_pending_dialogue_request", return_value=True):
        assert asyncio.run(broker._ensure_inner()) == "started"
    # owner now broker-started; flip regulation on and drain pending
    toolbox.active = True
    import tasks.model_service_activation as mod

    with patch.object(broker, "_has_pending_dialogue_request", return_value=False), patch.object(
        mod, "regulation_active", return_value=True
    ):
        assert asyncio.run(broker._ensure_inner()) == "released"
    log = (
        tmp_path
        / "main-system"
        / "runtime"
        / "logs"
        / "native-shadow"
        / "model-service-activation.jsonl"
    )
    assert _records(log) == []
