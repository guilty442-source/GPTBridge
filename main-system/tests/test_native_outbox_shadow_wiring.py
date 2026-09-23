"""§10.65 act-1 shadow wiring tests for state_outbox.

Covers flag gating, item-by-item comparison of hello/ack/drain-plan
decisions, divergence auditing, and fail-closed behaviour.  Native-
dependent cases skip gracefully when the governed extension predates
the E1 bindings.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from tasks.state_outbox_native_shadow import OutboxNativeShadow


def _write_policy(root: Path, mode: str) -> None:
    cfg = root / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text(
        json.dumps(
            {
                "schema": "native-shadow-policy/v1",
                "components": {"state_outbox": {"mode": mode}},
            }
        ),
        encoding="utf-8",
    )


def _native_available() -> bool:
    try:
        from core_system.native import _sovereign_native as native

        native.NativeOutbox()
    except Exception:
        return False
    return True


requires_native = pytest.mark.skipif(
    not _native_available(), reason="native pyd unavailable or predates E1 bindings"
)


def test_shadow_absent_without_policy(tmp_path: Path) -> None:
    assert OutboxNativeShadow.from_policy(tmp_path) is None


def test_shadow_absent_when_off(tmp_path: Path) -> None:
    _write_policy(tmp_path, "off")
    assert OutboxNativeShadow.from_policy(tmp_path) is None


def test_primary_mode_refused(tmp_path: Path) -> None:
    _write_policy(tmp_path, "primary")
    assert OutboxNativeShadow.from_policy(tmp_path) is None


class _StubOutbox:
    def __init__(self):
        self.calls = []
        self.ack_result = True
        self.plan = {"has_work": True, "start_after": 10, "limit": 100}
        self.hello_result = {"reset": False, "effective_cursor": 5}

    def register_session(self, sid):
        self.calls.append(("register", sid))
        return True

    def unregister_session(self, sid):
        self.calls.append(("unregister", sid))
        return True

    def hello(self, sid, cursor, gen_matches, latest):
        return dict(self.hello_result)

    def ack(self, sid, cursor):
        return self.ack_result

    def resync(self, sid, cursor):
        self.calls.append(("resync", sid, cursor))
        return True

    def drain_plan(self, sid, now_ms, retry_ms):
        return dict(self.plan)

    def mark_sent(self, sid, seq, now_ms):
        self.calls.append(("sent", sid, seq))
        return True

    def next_retry_deadline(self, retry_ms):
        return 0

    def prune_floor(self, latest):
        return 7


def test_hello_divergence_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = OutboxNativeShadow(log, _StubOutbox())
    shadow.observe_hello(
        "s1",
        cursor=5,
        generation_matches=True,
        latest_sequence=42,
        py_reset=True,
        py_cursor=42,
    )
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["kind"] == "divergence"
    assert rec["op"] == "hello"
    assert set(rec["mismatches"]) == {"reset", "effective_cursor"}
    assert rec["python"]["effective_cursor"] == 42
    assert rec["native"]["effective_cursor"] == 5


def test_ack_divergence_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubOutbox()
    stub.ack_result = False
    shadow = OutboxNativeShadow(log, stub)
    shadow.observe_ack("s1", cursor=10, py_accepted=True)
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["op"] == "ack"
    assert rec["python"]["accepted"] is True
    assert rec["native"]["accepted"] is False


def test_drain_plan_divergence_and_silent_match(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = OutboxNativeShadow(log, _StubOutbox())
    # match -> nothing written
    shadow.observe_drain_plan(
        "s1", py_window_open=True, py_start_after=10, py_limit=100
    )
    assert not log.exists()
    # mismatch -> divergence
    shadow.observe_drain_plan(
        "s1", py_window_open=True, py_start_after=10, py_limit=50
    )
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["op"] == "drain_plan"
    assert rec["mismatches"] == ["limit"]


def test_mark_sent_and_session_lifecycle(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubOutbox()
    shadow = OutboxNativeShadow(log, stub)
    shadow.observe_register("s1")
    shadow.observe_mark_sent("s1", sequence=42)
    shadow.observe_unregister("s1")
    assert ("register", "s1") in stub.calls
    assert ("sent", "s1", 42) in stub.calls
    assert ("unregister", "s1") in stub.calls
    assert not log.exists()


def test_prune_floor_divergence(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = OutboxNativeShadow(log, _StubOutbox())
    shadow.observe_prune_floor(py_floor=3, latest_sequence=99)
    rec = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
    assert rec["op"] == "prune_floor"
    assert rec["native"]["floor"] == 7


def test_native_error_fail_closed(tmp_path: Path) -> None:
    class _Boom:
        def ack(self, sid, cursor):
            raise RuntimeError("boom")

        def register_session(self, sid):
            return True

    log = tmp_path / "x.jsonl"
    shadow = OutboxNativeShadow(log, _Boom())
    for _ in range(3):
        shadow.observe_ack("s1", cursor=1, py_accepted=True)
    records = [
        json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["kind"] == "shadow-disabled"


@requires_native
def test_real_native_outbox_lockstep(tmp_path: Path) -> None:
    _write_policy(tmp_path, "shadow")
    shadow = OutboxNativeShadow.from_policy(tmp_path)
    assert shadow is not None
    log = (
        tmp_path
        / "main-system"
        / "runtime"
        / "logs"
        / "native-shadow"
        / "state-outbox.jsonl"
    )
    shadow.observe_register("s1")
    shadow.observe_hello(
        "s1", cursor=5, generation_matches=False, latest_sequence=42,
        py_reset=True, py_cursor=42,
    )
    shadow.observe_ack("s1", cursor=42, py_accepted=False)
    shadow.observe_drain_plan(
        "s1", py_window_open=True, py_start_after=42, py_limit=100
    )
    assert not log.exists() or not [
        line
        for line in log.read_text(encoding="utf-8").splitlines()
        if json.loads(line)["kind"] == "divergence"
    ]
