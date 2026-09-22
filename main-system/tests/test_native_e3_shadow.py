"""E3 native shadow bindings smoke tests (§10.65).

Same contract as test_native_e1_shadow / test_native_e2_shadow: verified
on a fresh ``_sovereign_native`` build; skips while the running backend
holds the locked .pyd on an older revision.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
sys.path.insert(0, str(SRC_CORE))

try:
    from core_system.native import _sovereign_native as _native

    _E3 = all(
        hasattr(_native, name)
        for name in ("NativeRuntimeStateRegistry", "NativeActivationBroker")
    )
except ImportError:
    _E3 = False

requires_e3 = pytest.mark.skipif(
    not _E3, reason="native pyd predates E3 bindings (rebuild pending)"
)


@requires_e3
def test_runtime_state_defaults_and_side_effects():
    reg = _native.NativeRuntimeStateRegistry()
    assert reg.count() == 0
    assert reg.heartbeat("mod-a", "t1")
    row = reg.get("mod-a")
    # Python ModuleRuntimeRecord defaults: STOPPED / AVAILABLE / unknown.
    assert row["runtime_state"] == "STOPPED"
    assert row["capability_state"] == "AVAILABLE"
    assert row["health"] == "unknown"
    assert row["last_heartbeat"] == "t1"
    assert row["updated_at"] == "t1"
    assert reg.get("missing") is None


@requires_e3
def test_runtime_state_recovering_and_ready_clears_error():
    reg = _native.NativeRuntimeStateRegistry()
    assert reg.set_runtime_state("m", "FAILED", None, None, "boom", "t0")
    assert reg.set_runtime_state("m", "RECOVERING", None, None, None, "t1")
    assert reg.set_runtime_state("m", "RECOVERING", None, None, None, "t2")
    row = reg.get("m")
    assert row["recovery_attempts"] == 2
    assert row["last_error"] == "boom"
    # READY with no error clears last_error (Python parity).
    assert reg.set_runtime_state("m", "READY", "ok", "rel-1", None, "t3")
    row = reg.get("m")
    assert row["last_error"] == ""
    assert row["health"] == "ok"
    assert row["release_id"] == "rel-1"
    # Unknown state names are rejected (ValueError parity -> False).
    assert not reg.set_runtime_state("m", "bogus", None, None, None, "t4")
    assert not reg.set_capability_state("m", "ready", "t4")


@requires_e3
def test_runtime_state_record_error_truncates_500():
    reg = _native.NativeRuntimeStateRegistry()
    assert reg.record_error("m", "x" * 600, "t0")
    assert len(reg.get("m")["last_error"]) == 500


@requires_e3
def test_runtime_state_aggregate_local_failure_isolation():
    reg = _native.NativeRuntimeStateRegistry()
    reg.set_runtime_state("zeta", "FAILED", None, None, None, "t")
    reg.set_runtime_state("alpha", "FAILED", None, None, None, "t")
    reg.set_runtime_state("beta", "READY", None, None, None, "t")
    reg.set_capability_state("beta", "DISABLED", "t")
    agg = reg.aggregate()
    assert agg["schema"] == "star-runtime-state/v1"
    assert agg["module_count"] == 3
    assert agg["by_runtime_state"]["FAILED"] == 2
    assert agg["by_runtime_state"]["READY"] == 1
    assert agg["by_capability_state"]["DISABLED"] == 1
    # Python sorted(failed_modules)
    assert agg["failed_modules"] == ["alpha", "zeta"]


def _pending(now=100.0):
    return {
        "pending": True,
        "maintenance_ready": True,
        "shutting_down": False,
        "admission_hold": False,
        "liveness_known": True,
        "owner_active": False,
        "regulation_active": False,
        "now_monotonic": now,
    }


@requires_e3
def test_activation_broker_admission_ladder():
    b = _native.NativeActivationBroker(20.0, 15.0, 180.0)
    in_ = _pending()
    in_["maintenance_ready"] = False
    assert b.ensure(in_) == "maintenance-pending"
    in_ = _pending()
    in_["shutting_down"] = True
    assert b.ensure(in_) == "shutting-down"
    in_ = _pending()
    in_["admission_hold"] = True
    assert b.ensure(in_) == "resource-hold"
    in_ = _pending()
    in_["liveness_known"] = False
    assert b.ensure(in_) == "liveness-unknown"
    in_ = _pending()
    in_["owner_active"] = True
    assert b.ensure(in_) == "owner-running"
    in_ = _pending()
    assert b.ensure(in_) == "should-start"
    assert b.status()["attempts"] == 1


@requires_e3
def test_activation_broker_throttle_backoff_and_release():
    b = _native.NativeActivationBroker(20.0, 15.0, 60.0)
    assert b.ensure(_pending(100.0)) == "should-start"
    assert b.on_start_result(True, 100.0) == "started"
    assert b.status()["broker_started_owner"] is True
    assert b.ensure(_pending(105.0)) == "throttled"
    assert b.ensure(_pending(121.0)) == "should-start"
    assert b.on_start_result(False, 121.0) == "start-failed"
    assert b.ensure(_pending(200.0)) == "should-start"
    b.on_start_result(False, 200.0)
    assert b.status()["backoff_seconds"] == 60.0  # doubled -> capped at max

    # Governed release: only broker-owned owner, only under regulation.
    idle = {
        "pending": False,
        "maintenance_ready": True,
        "shutting_down": False,
        "admission_hold": False,
        "liveness_known": True,
        "owner_active": True,
        "regulation_active": False,
        "now_monotonic": 300.0,
    }
    assert b.ensure(idle) == "idle"  # no regulation -> no release
    idle["regulation_active"] = True
    assert b.ensure(idle) == "should-release"
    assert b.on_release_result(True, 300.0) == "released"
    assert b.status()["broker_started_owner"] is False


@requires_e3
def test_activation_broker_explicit_stop_and_helpers():
    b = _native.NativeActivationBroker(20.0, 15.0, 180.0)
    b.note_explicit_stop(500.0, 9999.0)
    st = b.status()
    assert st["explicit_stop_at"] == 9999.0
    assert st["broker_started_owner"] is False
    assert b.ensure(_pending(505.0)) == "throttled"
    assert b.ensure(_pending(521.0)) == "should-start"
    # Static helpers (poll cadence + §10.63 R2 write throttle).
    assert _native.NativeActivationBroker.poll_interval(True, 5.0, 1.0) == 1.0
    assert _native.NativeActivationBroker.poll_interval(False, 5.0, 1.0) == 5.0
    assert _native.NativeActivationBroker.state_write_due(True, 10.0, 0.0, 60.0)
    assert not _native.NativeActivationBroker.state_write_due(
        False, 30.0, 0.0, 60.0
    )
    assert _native.NativeActivationBroker.state_write_due(
        False, 61.0, 0.0, 60.0
    )
