"""§10.21 Backend Lifecycle Contract tests — unified role+health split."""

from __future__ import annotations

import json

from core_system.backend_lifecycle import (
    BACKEND_LIFECYCLE_VERSION,
    HEALTH_DEGRADED,
    HEALTH_FAILED,
    HEALTH_READY,
    LIFECYCLE_ROLE_ACTIVE,
    LIFECYCLE_ROLE_DRAINING,
    LIFECYCLE_ROLE_STANDBY,
    LIFECYCLE_ROLE_STARTING,
    LIFECYCLE_ROLE_STOPPED,
    LIFECYCLE_ROLE_STOPPING,
    BackendLifecycleRegistry,
    health_transition_valid,
    role_transition_valid,
)


def test_role_and_health_transitions_valid():
    assert role_transition_valid(LIFECYCLE_ROLE_STARTING, LIFECYCLE_ROLE_ACTIVE)
    assert role_transition_valid(LIFECYCLE_ROLE_STARTING, LIFECYCLE_ROLE_STANDBY)
    assert role_transition_valid(LIFECYCLE_ROLE_STANDBY, LIFECYCLE_ROLE_ACTIVE)
    assert role_transition_valid(LIFECYCLE_ROLE_STANDBY, LIFECYCLE_ROLE_DRAINING)
    assert role_transition_valid(LIFECYCLE_ROLE_ACTIVE, LIFECYCLE_ROLE_DRAINING)
    assert role_transition_valid(LIFECYCLE_ROLE_DRAINING, LIFECYCLE_ROLE_STOPPING)
    assert role_transition_valid(LIFECYCLE_ROLE_DRAINING, LIFECYCLE_ROLE_STOPPED)
    assert not role_transition_valid(LIFECYCLE_ROLE_STOPPED, LIFECYCLE_ROLE_ACTIVE)
    assert not role_transition_valid(LIFECYCLE_ROLE_DRAINING, LIFECYCLE_ROLE_ACTIVE)
    assert health_transition_valid(HEALTH_READY, HEALTH_DEGRADED)
    assert health_transition_valid(HEALTH_DEGRADED, HEALTH_READY)


def test_registration_and_persistence(tmp_path):
    path = tmp_path / "lifecycle.json"
    reg = BackendLifecycleRegistry(path)
    ok = reg.register(
        "backend-a", release_id="rel-1", generation="gen-41"
    )
    assert ok.ok
    assert reg.get("backend-a") is not None

    reloaded = BackendLifecycleRegistry(path)
    state = reloaded.get("backend-a")
    assert state is not None
    assert state.release_id == "rel-1"
    assert state.generation == "gen-41"
    assert state.lifecycle_role == LIFECYCLE_ROLE_STARTING
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["backend_lifecycle_version"] == BACKEND_LIFECYCLE_VERSION


def test_duplicate_registration_reports_not_error(tmp_path):
    reg = BackendLifecycleRegistry(tmp_path / "lifecycle.json")
    reg.register("backend-a", generation="gen-1")
    again = reg.register("backend-a", generation="gen-2")
    assert again.ok is False
    assert again.reason == "already-registered"


def test_activerole_sets_accepting_flag(tmp_path):
    reg = BackendLifecycleRegistry(tmp_path / "lifecycle.json")
    reg.register("backend-a", generation="gen-1")
    reg.become_active("backend-a")
    state = reg.get("backend-a")
    assert state.lifecycle_role == LIFECYCLE_ROLE_ACTIVE
    assert state.accepting_new_requests is True


def test_standby_ready_coexist_with_active(tmp_path):
    """ACTIVE+READY 與 STANDBY+READY 並存皆合法（§10.21 核心）。"""
    reg = BackendLifecycleRegistry(tmp_path / "lifecycle.json")
    reg.register("backend-a", generation="gen-41")
    reg.become_active("backend-a")
    reg.register("backend-b", generation="gen-41")
    reg.become_standby("backend-b")

    active = reg.get("backend-a")
    standby = reg.get("backend-b")
    assert active.health_state == HEALTH_READY
    assert standby.health_state == HEALTH_READY
    assert active.lifecycle_role == LIFECYCLE_ROLE_ACTIVE
    assert standby.lifecycle_role == LIFECYCLE_ROLE_STANDBY
    both = reg.active_backends()
    assert {s.backend_id for s in both} == {"backend-a", "backend-b"}


def test_draining_stops_accepting_preserves_requests(tmp_path):
    reg = BackendLifecycleRegistry(tmp_path / "lifecycle.json")
    reg.register("backend-a", generation="gen-1")
    reg.become_active("backend-a")
    reg.heartbeat("backend-a", active_requests=5)
    reg.begin_draining("backend-a")
    state = reg.get("backend-a")
    assert state.lifecycle_role == LIFECYCLE_ROLE_DRAINING
    assert state.accepting_new_requests is False
    assert state.active_requests == 5  # 存量請求不因角色轉移被清空


def test_illegal_role_transition_fails_closed(tmp_path):
    reg = BackendLifecycleRegistry(tmp_path / "lifecycle.json")
    reg.register("backend-a", generation="gen-1")
    bad = reg.become_active("missing")
    assert bad.ok is False
    assert bad.reason == "backend-not-found"
    # DRAINING 是唯一能直接停機的收斂角色；STOPPED 後不得復活成 ACTIVE
    reg.become_active("backend-a")
    reg.begin_draining("backend-a")
    resurrect = reg.become_active("backend-a")
    assert resurrect.ok is False
    assert "illegal-role" in resurrect.reason
    assert reg.get("backend-a").lifecycle_role == LIFECYCLE_ROLE_DRAINING
    # STOPPED 之後接 STOPPED 甚至不可再跳 ACTIVE
    reg.begin_stopping("backend-a")
    reg.set_stopped("backend-a")
    assert reg.get("backend-a").lifecycle_role == LIFECYCLE_ROLE_STOPPED
    post = reg.become_active("backend-a")
    assert post.ok is False


def test_health_is_independent_from_role(tmp_path):
    reg = BackendLifecycleRegistry(tmp_path / "lifecycle.json")
    reg.register("backend-a", generation="gen-1")
    assert reg.mark_degraded("backend-a", error="leak").ok
    state = reg.get("backend-a")
    assert state.health_state == HEALTH_DEGRADED
    assert state.lifecycle_role == LIFECYCLE_ROLE_STARTING  # 角色不受影響
    assert state.last_error == "leak"
    assert reg.mark_ready("backend-a").ok


def test_failed_health_excluded_from_active_backends(tmp_path):
    reg = BackendLifecycleRegistry(tmp_path / "lifecycle.json")
    reg.register("backend-a", generation="gen-1")
    reg.become_active("backend-a")
    reg.mark_failed("backend-a", error="boom")
    assert reg.active_backends() == []


def test_heartbeat_updates_count_and_accepting(tmp_path):
    reg = BackendLifecycleRegistry(tmp_path / "lifecycle.json")
    reg.register("backend-a", generation="gen-1")
    reg.become_active("backend-a")
    hb = reg.heartbeat("backend-a", active_requests=3, accepting=True)
    assert hb.ok
    state = reg.get("backend-a")
    assert state.active_requests == 3
    assert state.accepting_new_requests is True
    missing = reg.heartbeat("missing", active_requests=1)
    assert missing.ok is False
    assert missing.reason == "backend-not-found"


def test_by_generation_and_snapshot(tmp_path):
    reg = BackendLifecycleRegistry(tmp_path / "lifecycle.json")
    reg.register("backend-a", generation="gen-1")
    reg.register("backend-b", generation="gen-2")
    gens1 = reg.by_generation("gen-1")
    assert [s.backend_id for s in gens1] == ["backend-a"]
    snap = reg.snapshot()
    assert snap["backend_lifecycle_version"] == BACKEND_LIFECYCLE_VERSION
    assert snap["count"] == 2