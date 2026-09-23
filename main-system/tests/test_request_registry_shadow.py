"""§10.65 act-1 shadow wiring tests for request_registry.

The harness runs the C ``NativeIpcRegistry`` request table in parallel
while Python stays authoritative; these tests cover flag parsing,
create/transition parity, admission-gap evidence (Python's §10.16
transition graph is stricter than the C terminal-lock rule), and
fail-closed disable behaviour.  Native-dependent cases skip when the
governed extension is not built.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from core_system.request_registry import RequestRegistry  # noqa: E402
from core_system.request_registry_native_shadow import (  # noqa: E402
    RequestRegistryNativeShadow,
)


def _write_policy(root: Path, mode: str) -> None:
    cfg = root / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text(
        json.dumps(
            {
                "schema": "native-shadow-policy/v1",
                "components": {"request_registry": {"mode": mode}},
            }
        ),
        encoding="utf-8",
    )


def _native_available() -> bool:
    try:
        from core_system.native import _sovereign_native as native

        native.NativeIpcRegistry()
        return True
    except Exception:
        return False


requires_native = pytest.mark.skipif(
    not _native_available(),
    reason="native pyd unavailable or predates NativeIpcRegistry",
)


def _records(path: Path) -> list:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


class _StubRegistry:
    """Controllable fake of ``NativeIpcRegistry``."""

    def __init__(self) -> None:
        self.rows = {}
        self.create_ok = True

    _NAMES = [
        "CREATED", "QUEUED", "RUNNING", "COMPLETED",
        "FAILED", "CANCELLED", "TIMED_OUT", "INTERRUPTED",
    ]

    def create(self, request_id, generation, now_ms=1000):
        if not self.create_ok or request_id in self.rows:
            return False
        self.rows[request_id] = {
            "status": "CREATED",
            "cancelled": False,
            "created_at_ms": now_ms,
            "started_at_ms": 0,
            "completed_at_ms": 0,
            "timeout_ms": 0,
        }
        return True

    def set_status(self, request_id, status, now_ms=2000):
        row = self.rows.get(request_id)
        if row is None:
            return False
        if row["status"] in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
            if self._NAMES[status] != row["status"]:
                return False
        row["status"] = self._NAMES[status]
        if row["status"] == "RUNNING" and not row["started_at_ms"]:
            row["started_at_ms"] = now_ms
        if row["status"] in ("COMPLETED", "FAILED", "TIMED_OUT") and not row[
            "completed_at_ms"
        ]:
            row["completed_at_ms"] = now_ms
        return True

    def cancel(self, request_id, now_ms=2000):
        row = self.rows.get(request_id)
        if row is None:
            return False
        row["status"] = "CANCELLED"
        row["cancelled"] = True
        if not row["completed_at_ms"]:
            row["completed_at_ms"] = now_ms
        return True

    def set_timeout(self, request_id, timeout_ms=0):
        row = self.rows.get(request_id)
        if row is None or timeout_ms < 0:
            return False
        row["timeout_ms"] = timeout_ms
        return True

    def find(self, request_id):
        row = self.rows.get(request_id)
        if row is None:
            return None
        return {
            "request_id": request_id,
            "status": row["status"],
            "cancelled": row["cancelled"],
            "created_at_ms": row["created_at_ms"],
            "started_at_ms": row["started_at_ms"],
            "completed_at_ms": row["completed_at_ms"],
            "timeout_ms": row["timeout_ms"],
            "deadline_ms": (
                row["created_at_ms"] + row["timeout_ms"]
                if row["timeout_ms"] > 0
                else 0
            ),
        }

    def count(self):
        return len(self.rows)


class _RaisingRegistry:
    def create(self, *a, **k):
        raise RuntimeError("native boom")

    def set_status(self, *a, **k):
        raise RuntimeError("native boom")

    def cancel(self, *a, **k):
        raise RuntimeError("native boom")

    def find(self, *a, **k):
        raise RuntimeError("native boom")


# --- policy gate ---


def test_from_policy_absent_without_file(tmp_path: Path) -> None:
    assert RequestRegistryNativeShadow.from_policy(tmp_path) is None


def test_from_policy_absent_when_off(tmp_path: Path) -> None:
    _write_policy(tmp_path, "off")
    assert RequestRegistryNativeShadow.from_policy(tmp_path) is None


def test_from_policy_refuses_primary(tmp_path: Path) -> None:
    _write_policy(tmp_path, "primary")
    assert RequestRegistryNativeShadow.from_policy(tmp_path) is None


def test_from_policy_absent_on_invalid_json(tmp_path: Path) -> None:
    cfg = tmp_path / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text("{ nope", encoding="utf-8")
    assert RequestRegistryNativeShadow.from_policy(tmp_path) is None


# --- create / status parity ---


def test_create_refusal_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubRegistry()
    stub.create_ok = False
    shadow = RequestRegistryNativeShadow(log, stub)
    shadow.observe_create("r1", "7")
    recs = _records(log)
    assert [r["op"] for r in recs] == ["create"]


def test_status_match_silent(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = RequestRegistryNativeShadow(log, _StubRegistry())
    shadow.observe_create("r1", "7")
    shadow.observe_status("r1", "RUNNING", py_ok=True)
    assert not log.exists()


def test_status_native_refusal_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubRegistry()
    shadow = RequestRegistryNativeShadow(log, stub)
    # native has no row -> set_status refuses while Python accepted
    shadow.observe_status("ghost", "RUNNING", py_ok=True)
    recs = _records(log)
    assert [r["op"] for r in recs] == ["set_status"]


def test_cancel_routes_to_native_cancel(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubRegistry()
    shadow = RequestRegistryNativeShadow(log, stub)
    shadow.observe_create("r1", "0")
    shadow.observe_status("r1", "CANCELLED", py_ok=True)
    assert stub.rows["r1"]["status"] == "CANCELLED"
    assert stub.rows["r1"]["cancelled"] is True
    assert not log.exists()


# --- refusal / admission-gap evidence ---


def test_refused_admission_gap_when_native_would_allow(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubRegistry()
    stub.create("r1", 0)
    shadow = RequestRegistryNativeShadow(log, stub)
    # Python refuses CREATED->COMPLETED; C terminal-lock would allow it
    shadow.observe_refused("r1", "COMPLETED", reason="invalid-request-transition")
    recs = _records(log)
    assert [r["op"] for r in recs] == ["admission-gap"]
    assert recs[0]["native"]["would_accept"] is True


def test_refused_terminal_lock_silent(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    stub = _StubRegistry()
    stub.create("r1", 0)
    stub.set_status("r1", 2)  # RUNNING
    stub.set_status("r1", 3)  # COMPLETED (terminal)
    shadow = RequestRegistryNativeShadow(log, stub)
    shadow.observe_refused("r1", "RUNNING", reason="request-terminal")
    assert not log.exists()


def test_refused_membership_gap_recorded(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = RequestRegistryNativeShadow(log, _StubRegistry())
    shadow.observe_refused("ghost", "RUNNING", reason="request-not-found")
    recs = _records(log)
    assert [r["op"] for r in recs] == ["refused"]
    assert recs[0]["native"]["present"] is False


# --- fail-closed ---


def test_native_error_disables_once(tmp_path: Path) -> None:
    log = tmp_path / "x.jsonl"
    shadow = RequestRegistryNativeShadow(log, _RaisingRegistry())
    for _ in range(3):
        shadow.observe_create("r", "0")
    recs = _records(log)
    assert len(recs) == 1
    assert recs[0]["kind"] == "shadow-disabled"
    assert recs[0]["reason"] == "native-create-error"


# --- registry wiring ---


class _RecordingShadow:
    def __init__(self) -> None:
        self.calls = []

    def observe_create(self, request_id, backend_generation):
        self.calls.append(("create", request_id, backend_generation))

    def observe_status(self, request_id, status, *, py_ok):
        self.calls.append(("status", request_id, status, py_ok))

    def observe_refused(self, request_id, status, *, reason):
        self.calls.append(("refused", request_id, status, reason))

    def observe_timeout(self, request_id, timeout_s):
        self.calls.append(("timeout", request_id, timeout_s))


def test_registry_no_shadow_without_project_root(tmp_path: Path) -> None:
    reg = RequestRegistry(tmp_path / "req.json")
    assert reg._native_shadow is None


def test_registry_hooks_reach_shadow(tmp_path: Path) -> None:
    reg = RequestRegistry(tmp_path / "req.json")
    rec = _RecordingShadow()
    reg._native_shadow = rec

    reg.upsert("r1", backend_generation="3")
    assert ("create", "r1", "3") in rec.calls

    reg.mark_started("r1")
    assert ("status", "r1", "RUNNING", True) in rec.calls

    res = reg.update("r1", status="QUEUED")  # RUNNING->QUEUED invalid
    assert res.ok is False
    refused = [c for c in rec.calls if c[0] == "refused"]
    assert refused and refused[0][1] == "r1"

    reg.mark_cancelled("r1")
    assert ("status", "r1", "CANCELLED", True) in rec.calls


def test_registry_upsert_status_rewrite_observed(tmp_path: Path) -> None:
    reg = RequestRegistry(tmp_path / "req.json")
    rec = _RecordingShadow()
    reg._native_shadow = rec
    reg.upsert("r1")
    reg.upsert("r1", status="QUEUED")  # merge path rewrites status
    assert ("status", "r1", "QUEUED", True) in rec.calls


@requires_native
def test_real_native_lockstep(tmp_path: Path) -> None:
    _write_policy(tmp_path, "shadow")
    reg = RequestRegistry(tmp_path / "req.json", project_root=tmp_path)
    assert reg._native_shadow is not None

    reg.upsert("r1", backend_generation="4")
    reg.mark_started("r1")
    reg.mark_completed("r1")
    log = (
        tmp_path
        / "main-system"
        / "runtime"
        / "logs"
        / "native-shadow"
        / "request-registry.jsonl"
    )
    assert _records(log) == []


@requires_native
def test_real_native_admission_gap(tmp_path: Path) -> None:
    _write_policy(tmp_path, "shadow")
    reg = RequestRegistry(tmp_path / "req.json", project_root=tmp_path)
    reg.upsert("r1")
    # CREATED -> COMPLETED is refused by the §10.16 graph but the C
    # terminal-lock rule would accept it -> admission-gap evidence.
    res = reg.update("r1", status="COMPLETED")
    assert res.ok is False
    log = (
        tmp_path
        / "main-system"
        / "runtime"
        / "logs"
        / "native-shadow"
        / "request-registry.jsonl"
    )
    recs = _records(log)
    assert [r["op"] for r in recs] == ["admission-gap"]
