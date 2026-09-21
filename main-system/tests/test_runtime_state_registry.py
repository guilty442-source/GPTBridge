"""G47 unified runtime state registry tests (star-runtime-state/v1)."""
from __future__ import annotations

import pytest

from core_system.runtime_state_registry import (
    CAPABILITY_STATES,
    RUNTIME_STATES,
    RuntimeStateRegistry,
)


def test_register_and_query(tmp_path):
    reg = RuntimeStateRegistry(tmp_path / "state.json")
    reg.set_runtime_state("rag", "READY", health="ok", release_id="rel-1")
    reg.set_capability_state("rag", "AVAILABLE")
    rec = reg.get("rag")
    assert rec is not None
    assert rec.runtime_state == "READY"
    assert rec.capability_state == "AVAILABLE"
    assert rec.release_id == "rel-1"


def test_unknown_states_rejected(tmp_path):
    reg = RuntimeStateRegistry(tmp_path / "state.json")
    with pytest.raises(ValueError):
        reg.set_runtime_state("m", "BOGUS")
    with pytest.raises(ValueError):
        reg.set_capability_state("m", "BOGUS")
    assert set(RUNTIME_STATES) == {
        "STARTING", "READY", "DEGRADED", "RECOVERING", "FAILED",
        "STOPPING", "STOPPED",
    }
    assert set(CAPABILITY_STATES) == {
        "AVAILABLE", "UNAVAILABLE", "DISABLED", "NOT_INSTALLED",
    }


def test_recovery_attempts_counted(tmp_path):
    reg = RuntimeStateRegistry(tmp_path / "state.json")
    reg.set_runtime_state("db", "RECOVERING")
    reg.set_runtime_state("db", "RECOVERING")
    assert reg.get("db").recovery_attempts == 2


def test_error_recorded_and_cleared_on_ready(tmp_path):
    reg = RuntimeStateRegistry(tmp_path / "state.json")
    reg.record_error("qdrant", "connection refused")
    assert reg.get("qdrant").last_error == "connection refused"
    reg.set_runtime_state("qdrant", "READY")
    assert reg.get("qdrant").last_error == ""


def test_local_failure_does_not_propagate(tmp_path):
    reg = RuntimeStateRegistry(tmp_path / "state.json")
    reg.set_runtime_state("rag", "READY")
    reg.set_runtime_state("qdrant", "FAILED", error="crash")
    agg = reg.aggregate()
    assert agg["module_count"] == 2
    assert agg["by_runtime_state"]["READY"] == 1
    assert agg["by_runtime_state"]["FAILED"] == 1
    assert agg["failed_modules"] == ["qdrant"]
    # the healthy module is untouched by the failure
    assert reg.get("rag").runtime_state == "READY"


def test_persist_and_reload(tmp_path):
    path = tmp_path / "state.json"
    reg = RuntimeStateRegistry(path)
    reg.set_runtime_state("a", "READY", release_id="r1")
    reg.heartbeat("a")
    reg2 = RuntimeStateRegistry(path)
    rec = reg2.get("a")
    assert rec.runtime_state == "READY"
    assert rec.release_id == "r1"
    assert rec.last_heartbeat


def test_corrupt_state_does_not_crash(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    reg = RuntimeStateRegistry(path)  # must not raise
    assert reg.aggregate()["module_count"] == 0
