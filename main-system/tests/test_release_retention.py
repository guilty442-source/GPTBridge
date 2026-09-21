"""§10.15 Release Retention hierarchy tests — ACTIVE/PREVIOUS/ROLLBACK/ARCHIVE."""

from __future__ import annotations

import json

from core_system.release_retention import (
    RETENTION_VERSION,
    TIER_ACTIVE,
    TIER_ARCHIVE,
    TIER_PREVIOUS,
    TIER_ROLLBACK,
    ReleaseRetentionRegistry,
    tier_transition_valid,
)


def test_tier_transition_matrix():
    assert tier_transition_valid(TIER_ACTIVE, TIER_PREVIOUS)
    assert tier_transition_valid(TIER_PREVIOUS, TIER_ACTIVE)
    assert tier_transition_valid(TIER_PREVIOUS, TIER_ARCHIVE)
    assert tier_transition_valid(TIER_PREVIOUS, TIER_ROLLBACK)
    assert tier_transition_valid(TIER_ROLLBACK, TIER_ACTIVE)
    assert tier_transition_valid(TIER_ROLLBACK, TIER_ARCHIVE)
    assert not tier_transition_valid(TIER_ARCHIVE, TIER_PREVIOUS)
    assert not tier_transition_valid(TIER_ARCHIVE, TIER_ROLLBACK)
    assert not tier_transition_valid(TIER_ACTIVE, TIER_ARCHIVE)


def test_register_default_archive(tmp_path):
    reg = ReleaseRetentionRegistry(tmp_path / "r.json", audit_path=tmp_path / "r.jsonl")
    result = reg.register("release-1", application_version="1.0.0")
    assert result.ok
    entry = reg.get("release-1")
    assert entry.tier == TIER_ARCHIVE
    assert entry.application_version == "1.0.0"


def test_single_active_invariant(tmp_path):
    """昇級時永遠只有一個 ACTIVE：舊 ACTIVE 自動降為 PREVIOUS。"""
    reg = ReleaseRetentionRegistry(tmp_path / "r.json", audit_path=tmp_path / "r.jsonl")
    reg.register("release-1")
    reg.register("release-2")
    reg.become_active("release-1")
    reg.become_active("release-2")
    active = reg.active_release()
    assert active is not None
    assert active.release_id == "release-2"
    assert reg.get("release-1").tier == TIER_PREVIOUS
    actives = [e for e in reg._entries.values() if e.tier == TIER_ACTIVE]
    assert len(actives) == 1


def test_full_promotion_demotion_cycle(tmp_path):
    """release-1 ACTIVE → release-2 取代 → release-1 降 PREVIOUS。"""
    reg = ReleaseRetentionRegistry(tmp_path / "r.json", audit_path=tmp_path / "r.jsonl")
    reg.register("release-1", application_version="1.0.0")
    reg.register("release-2", application_version="2.0.0")
    # 先昇級 release-1 為 ACTIVE，再昇級 release-2（release-1 需先 PREVIOUS）
    reg.become_active("release-1")
    reg.demote_to_previous("release-1")
    reg.become_active("release-2")
    assert reg.active_release().release_id == "release-2"
    assert [r.release_id for r in reg.previous_releases()] == ["release-1"]


def test_archive_requires_all_three_gates(tmp_path):
    reg = ReleaseRetentionRegistry(tmp_path / "r.json", audit_path=tmp_path / "r.jsonl")
    reg.register("release-1", application_version="1.0.0")
    reg.become_active("release-1")
    reg.demote_to_previous("release-1")

    # 無任何閘門 → 拒絕
    denied = reg.archive("release-1")
    assert denied.ok is False
    assert denied.reason == "observation-window-not-passed"

    # 只過觀察窗 → 仍拒絕（備份未驗證）
    reg.mark_observation_passed("release-1")
    denied = reg.archive("release-1")
    assert denied.ok is False
    assert denied.reason == "backup-not-verified"

    # 補備份 → 仍拒絕（回復路徑未確認）
    reg.mark_backup_verified("release-1")
    denied = reg.archive("release-1")
    assert denied.ok is False
    assert denied.reason == "rollback-path-not-confirmed"

    # 三閘門全過 → ARCHIVE
    reg.mark_rollback_path_confirmed("release-1")
    ok = reg.archive("release-1")
    assert ok.ok
    assert reg.get("release-1").tier == TIER_ARCHIVE


def test_update_success_does_not_purge_previous(tmp_path):
    """§10.15：不得以更新成功直接清除全部舊版。"""
    reg = ReleaseRetentionRegistry(tmp_path / "r.json", audit_path=tmp_path / "r.jsonl")
    reg.register("release-1")
    reg.become_active("release-1")
    reg.demote_to_previous("release-1")
    reg.register("release-2")
    reg.become_active("release-2")
    # 更新成功後 release-1 仍在 PREVIOUS（不得刪除）
    previous = reg.previous_releases()
    assert len(previous) == 1
    assert previous[0].release_id == "release-1"
    assert previous[0].tier == TIER_PREVIOUS
    assert reg.get("release-1").tier == TIER_PREVIOUS


def test_rollback_requires_compatible_previous(tmp_path):
    reg = ReleaseRetentionRegistry(tmp_path / "r.json", audit_path=tmp_path / "r.jsonl")
    reg.register("release-1", application_version="1.0.0")
    reg.register("release-2", application_version="2.0.0")
    reg.become_active("release-1")
    # release-2 昇級時標記可與 release-1 相容；舊 ACTIVE(release-1)自動降 PREVIOUS
    reg.become_active("release-2", compatible_with=["release-1"])
    assert reg.active_release().release_id == "release-2"
    assert reg.get("release-1").tier == TIER_PREVIOUS

    # fallback release-1 相容 current release-2 → 可回復
    rollback = reg.rollback_to("release-2", "release-1")
    assert rollback.ok
    assert reg.active_release().release_id == "release-1"
    # 現 ACTIVE 已降為 PREVIOUS
    assert reg.get("release-2").tier == TIER_PREVIOUS


def test_rollback_rejects_incompatible(tmp_path):
    reg = ReleaseRetentionRegistry(tmp_path / "r.json", audit_path=tmp_path / "r.jsonl")
    reg.register("release-1")
    reg.register("release-2")
    reg.become_active("release-1")
    reg.become_active("release-2")  # 未標記相容
    result = reg.rollback_to("release-2", "release-1")
    assert result.ok is False
    assert result.reason == "fallback-not-compatible"


def test_persistence_and_audit(tmp_path):
    state = tmp_path / "r.json"
    audit = tmp_path / "r.jsonl"
    reg = ReleaseRetentionRegistry(state, audit_path=audit)
    reg.register("release-1", application_version="1.0.0")
    reg.become_active("release-1")
    reg.demote_to_previous("release-1")

    reloaded = ReleaseRetentionRegistry(state, audit_path=audit)
    assert reloaded.get("release-1").tier == TIER_PREVIOUS
    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["retention_version"] == RETENTION_VERSION
    # 審計有登錄（register/become-active/demote）
    lines = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines() if line]
    ops = {line["operation"] for line in lines}
    assert {"register", "become-active", "demote-to-previous"} <= ops


def test_snapshot_reports_tiers(tmp_path):
    reg = ReleaseRetentionRegistry(tmp_path / "r.json", audit_path=tmp_path / "r.jsonl")
    reg.register("release-1")
    rej = reg.register("release-1")
    assert rej.ok is False
    assert rej.reason == "already-registered"
    snap = reg.snapshot()
    assert snap["retention_version"] == RETENTION_VERSION
    assert snap["count"] == 1