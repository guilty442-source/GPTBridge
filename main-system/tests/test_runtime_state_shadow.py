"""§10.65 act-1 shadow wiring tests for runtime_state_registry.

The harness runs the C ``NativeRuntimeStateRegistry`` module table in
parallel while Python stays authoritative; these tests cover flag
parsing, per-mutation record parity, aggregate parity, divergence
evidence, and fail-closed disable behaviour.  Native-dependent cases
skip when the governed extension is not built.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from core_system.runtime_state_registry import (  # noqa: E402
    RuntimeStateRegistry,
)
from core_system.runtime_state_native_shadow import (  # noqa: E402
    RuntimeStateNativeShadow,
)


def _write_policy(root: Path, mode: str) -> None:
    cfg = root / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text(
        json.dumps(
            {
                "schema": "native-shadow-policy/v1",
                "components": {"runtime_state_registry": {"mode": mode}},
            }
        ),
        encoding="utf-8",
    )


def _native_available() -> bool:
    try:
        from core_system.native import _sovereign_native as native

        native.NativeRuntimeStateRegistry()
        return True
    except Exception:
        return False


requires_native = pytest.mark.skipif(
    not _native_available(),
    reason="native pyd unavailable or predates NativeRuntimeStateRegistry",
)


def _records(path: Path) -> list:
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


class _StubRegistry:
    """Controllable fake of ``NativeRuntimeStateRegistry``."""

    def __init__(self) -> None:
        self.rows = {}
        self.ok = True

    def _row(self, module_id):
        return self.rows.setdefault(
            module_id,
            {
                "module_id": module_id,
                "runtime_state": "STOPPED",
                "capability_state": "AVAILABLE",
                "health": "unknown",
                "release_id": "",
                "last_heartbeat": "",
                "last_error": "",
                "recovery_attempts": 0,
                "updated_at": "",
            },
        )

    def set_runtime_state(
        self, module_id, state, health, release_id, error, now_str
    ):
        if not self.ok:
            return False
        row = self._row(module_id)
        row["runtime_state"] = state
        if state == "RECOVERING":
            row["recovery_attempts"] += 1
        if health is not None:
            row["health"] = health
        if release_id is not None:
            row["release_id"] = release_id
        if error is not None:
            row["last_error"] = error
        elif state in ("READY", "STARTING"):
            row["last_error"] = ""
        row["updated_at"] = now_str
        return True

    def set_capability_state(self, module_id, state, now_str):
        if not self.ok:
            return False
        row = self._row(module_id)
        row["capability_state"] = state
        row["updated_at"] = now_str
        return True

    def heartbeat(self, module_id, now_str):
        if not self.ok:
            return False
        row = self._row(module_id)
        row["last_heartbeat"] = now_str
        row["updated_at"] = now_str
        return True

    def record_error(self, module_id, error, now_str):
        if not self.ok:
            return False
        row = self._row(module_id)
        row["last_error"] = error[:500]
        row["updated_at"] = now_str
        return True

    def get(self, module_id):
        return self.rows.get(module_id)

    def aggregate(self):
        by_rt = {}
        by_cap = {}
        failed = []
        for row in self.rows.values():
            by_rt[row["runtime_state"]] = by_rt.get(row["runtime_state"], 0) + 1
            by_cap[row["capability_state"]] = (
                by_cap.get(row["capability_state"], 0) + 1
            )
            if row["runtime_state"] == "FAILED":
                failed.append(row["module_id"])
        return {
            "module_count": len(self.rows),
            "by_runtime_state": by_rt,
            "by_capability_state": by_cap,
            "failed_modules": failed,
        }

    def count(self):
        return len(self.rows)


class _ExplodingRegistry(_StubRegistry):
    def set_runtime_state(
        self, module_id, state, health, release_id, error, now_str
    ):
        raise RuntimeError("native boom")


def _registry(tmp_path: Path) -> RuntimeStateRegistry:
    return RuntimeStateRegistry(tmp_path / "state.json")


# ---------------------------------------------------------------- policy


def test_no_policy_no_shadow(tmp_path):
    assert RuntimeStateNativeShadow.from_policy(tmp_path) is None


def test_off_mode_no_shadow(tmp_path):
    _write_policy(tmp_path, "off")
    assert RuntimeStateNativeShadow.from_policy(tmp_path) is None


def test_primary_mode_refused(tmp_path):
    _write_policy(tmp_path, "primary")
    assert RuntimeStateNativeShadow.from_policy(tmp_path) is None


def test_retire_mode_refused(tmp_path):
    _write_policy(tmp_path, "retire")
    assert RuntimeStateNativeShadow.from_policy(tmp_path) is None


def test_invalid_policy_json_no_shadow(tmp_path):
    cfg = tmp_path / "main-system" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "native-shadow.json").write_text("{not json", encoding="utf-8")
    assert RuntimeStateNativeShadow.from_policy(tmp_path) is None


def test_registry_without_project_root_has_no_shadow(tmp_path):
    reg = _registry(tmp_path)
    assert reg._native_shadow is None
    # Python path fully usable without the shadow
    rec = reg.set_runtime_state("m1", "READY", health="ok")
    assert rec.runtime_state == "READY"


# ------------------------------------------------------------ stub parity


def _stub_shadow(tmp_path):
    stub = _StubRegistry()
    shadow = RuntimeStateNativeShadow(
        tmp_path / "audit" / "runtime-state.jsonl", stub
    )
    return shadow, stub


def test_stub_lockstep_clean_log(tmp_path):
    reg = _registry(tmp_path)
    shadow, stub = _stub_shadow(tmp_path)
    reg._native_shadow = shadow

    reg.set_runtime_state(
        "mod-a", "STARTING", health="starting", release_id="r1"
    )
    reg.set_runtime_state("mod-a", "READY", health="ok")
    reg.set_capability_state("mod-a", "UNAVAILABLE")
    reg.heartbeat("mod-a")
    reg.record_error("mod-a", "boom")
    reg.set_runtime_state("mod-b", "FAILED", error="dead")
    reg.aggregate()

    assert stub.count() == 2
    assert _records(shadow._log_path) == []


def test_stub_divergence_logged(tmp_path):
    reg = _registry(tmp_path)
    shadow, stub = _stub_shadow(tmp_path)
    reg._native_shadow = shadow

    reg.set_runtime_state("mod-a", "READY", health="ok")
    # Corrupt native view: flip capability state behind Python's back
    stub.rows["mod-a"]["capability_state"] = "DISABLED"
    reg.set_capability_state("mod-a", "UNAVAILABLE")

    rows = _records(shadow._log_path)
    assert len(rows) == 0  # stub followed the mutation — now equal

    stub.rows["mod-a"]["last_error"] = "stale"
    reg.record_error("mod-a", "fresh")
    rows = _records(shadow._log_path)
    assert len(rows) == 0

    stub.rows["mod-a"]["health"] = "wrong"
    reg.set_runtime_state("mod-b", "READY")
    rows = _records(shadow._log_path)
    assert len(rows) == 0

    # Force a mismatch visible at next compare
    stub.rows["mod-b"]["runtime_state"] = "STOPPED"
    reg.heartbeat("mod-b")  # heartbeat does not compare
    reg.set_capability_state("mod-b", "DISABLED")  # compare fires here
    rows = _records(shadow._log_path)
    assert any(
        r["kind"] == "divergence" and "runtime_state" in r["mismatches"]
        for r in rows
    )


def test_stub_aggregate_divergence(tmp_path):
    reg = _registry(tmp_path)
    shadow, stub = _stub_shadow(tmp_path)
    reg._native_shadow = shadow

    reg.set_runtime_state("mod-a", "FAILED", error="x")
    stub.rows["mod-a"]["runtime_state"] = "READY"
    reg.aggregate()

    rows = _records(shadow._log_path)
    agg = [r for r in rows if r["kind"] == "divergence" and r["op"] == "aggregate"]
    assert agg
    assert "by_runtime_state" in agg[-1]["mismatches"]


def test_stub_native_reject_logged(tmp_path):
    reg = _registry(tmp_path)
    shadow, stub = _stub_shadow(tmp_path)
    reg._native_shadow = shadow

    stub.ok = False
    reg.set_runtime_state("mod-a", "READY")
    rows = _records(shadow._log_path)
    assert len(rows) == 1
    assert rows[0]["native"] == {"accepted": False}


# ---------------------------------------------------------- fail-closed


def test_native_exception_disables_once(tmp_path):
    reg = _registry(tmp_path)
    shadow = RuntimeStateNativeShadow(
        tmp_path / "audit" / "runtime-state.jsonl", _ExplodingRegistry()
    )
    reg._native_shadow = shadow

    rec = reg.set_runtime_state("mod-a", "READY")  # Python unaffected
    assert rec.runtime_state == "READY"
    reg.set_runtime_state("mod-a", "FAILED")  # second failure, no new record
    reg.set_capability_state("mod-a", "DISABLED")

    rows = _records(shadow._log_path)
    assert len(rows) == 1
    assert rows[0]["kind"] == "shadow-disabled"


# ------------------------------------------------------ real-native parity


@requires_native
def test_real_native_lockstep(tmp_path):
    from core_system.native import _sovereign_native as native

    reg = _registry(tmp_path)
    shadow = RuntimeStateNativeShadow(
        tmp_path / "audit" / "runtime-state.jsonl",
        native.NativeRuntimeStateRegistry(),
    )
    reg._native_shadow = shadow

    reg.set_runtime_state(
        "mod-a", "STARTING", health="starting", release_id="r1"
    )
    reg.set_runtime_state("mod-a", "READY", health="ok")
    reg.set_runtime_state("mod-b", "RECOVERING")
    reg.set_runtime_state("mod-b", "RECOVERING")
    reg.set_capability_state("mod-b", "DISABLED")
    reg.heartbeat("mod-a")
    reg.record_error("mod-b", "crash-loop")
    reg.set_runtime_state("mod-b", "FAILED", error="giving up")
    reg.aggregate()

    rows = _records(shadow._log_path)
    assert rows == [], f"unexpected divergences: {rows}"


@requires_native
def test_real_native_policy_construction(tmp_path):
    _write_policy(tmp_path, "shadow")
    shadow = RuntimeStateNativeShadow.from_policy(tmp_path)
    assert shadow is not None
