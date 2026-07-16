from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "platform_tools"
    / "file-sorter"
    / "src"
    / "main.py"
)
SPEC = importlib.util.spec_from_file_location("file_sorter_main_v2_tests", MODULE_PATH)
assert SPEC and SPEC.loader
file_sorter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = file_sorter
SPEC.loader.exec_module(file_sorter)
sorter_v2 = sys.modules["sorter_v2"]


@pytest.fixture(autouse=True)
def isolate_legacy_rules(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    rules_path = tmp_path.parent / f"{tmp_path.name}-v2-keyword-rules.json"
    monkeypatch.setattr(file_sorter, "RULES_FILE_PATH", rules_path)
    monkeypatch.setattr(
        file_sorter,
        "LEGACY_RULES_MIGRATION_INBOX_DIR",
        tmp_path / "legacy-rule-migration-inbox",
    )


def test_preview_skips_partial_and_recent_files_without_moving(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    destination = tmp_path / "music"
    destination.mkdir()
    partial = tmp_path / "music-track.crdownload"
    recent = tmp_path / "music-track.mp3"
    partial.write_text("partial", encoding="utf-8")
    recent.write_text("recent", encoding="utf-8")

    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=60,
        state_root=state_root,
    )
    payload = plan.to_dict()

    assert payload["plan_id"]
    assert payload["summary"]["ready"] == 0
    assert payload["summary"]["unstable"] == 2
    assert {item["reason"] for item in payload["skipped"]} == {
        "partial-file-suffix",
        "quiet-period",
    }
    assert partial.exists()
    assert recent.exists()
    assert (state_root / "plans" / f"{plan.plan_id}.json").exists()


def test_preview_apply_is_idempotent_and_undo_restores_source(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    destination = tmp_path / "music"
    destination.mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_bytes(b"durable payload")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
    )

    first = file_sorter.apply_organize_plan(
        plan.plan_id,
        target_dir=tmp_path,
        state_root=state_root,
    )
    replay = file_sorter.apply_organize_plan(
        plan.plan_id,
        target_dir=tmp_path,
        state_root=state_root,
    )

    assert first["ok"] is True
    assert first["moved_count"] == 1
    assert replay["transaction_id"] == first["transaction_id"]
    assert replay["replayed"] is True
    assert not source.exists()
    assert (destination / source.name).read_bytes() == b"durable payload"

    undone = sorter_v2.undo_last_transaction(tmp_path, state_root=state_root)

    assert undone["ok"] is True
    assert undone["undone_count"] == 1
    assert source.read_bytes() == b"durable payload"
    assert not (destination / source.name).exists()


def test_apply_refuses_destination_conflict_without_overwrite(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    destination = tmp_path / "music"
    destination.mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_text("source", encoding="utf-8")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
    )
    planned_destination = Path(plan.operations[0].destination)
    planned_destination.write_text("appeared later", encoding="utf-8")

    result = file_sorter.apply_organize_plan(
        plan.plan_id,
        target_dir=tmp_path,
        state_root=state_root,
    )

    assert result["ok"] is False
    assert result["moved_count"] == 0
    assert source.read_text(encoding="utf-8") == "source"
    assert planned_destination.read_text(encoding="utf-8") == "appeared later"


def test_apply_refuses_source_changed_after_preview(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    (tmp_path / "music").mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_text("before", encoding="utf-8")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
    )
    source.write_text("changed and longer", encoding="utf-8")

    result = file_sorter.apply_organize_plan(
        plan.plan_id,
        target_dir=tmp_path,
        state_root=state_root,
    )

    assert result["ok"] is False
    assert result["moved_count"] == 0
    assert source.read_text(encoding="utf-8") == "changed and longer"


def test_execute_refuses_expired_plan(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    (tmp_path / "music").mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_text("source", encoding="utf-8")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
        persist=False,
    )
    plan.expires_at = "2000-01-01T00:00:00+00:00"

    with pytest.raises(sorter_v2.SorterV2Error, match="expired"):
        sorter_v2.execute_plan(plan, state_root=state_root)

    assert source.read_text(encoding="utf-8") == "source"


def test_forced_staged_transfer_verifies_hash_before_source_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    destination = tmp_path / "archive"
    destination.mkdir()
    source = tmp_path / "archive-video.bin"
    source.write_bytes(b"verified bytes" * 1024)
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
    )
    monkeypatch.setattr(sorter_v2, "same_volume", lambda *_: False)

    result = file_sorter.apply_organize_plan(
        plan.plan_id,
        target_dir=tmp_path,
        state_root=state_root,
    )
    journal = json.loads(Path(result["journal_path"]).read_text(encoding="utf-8"))

    assert result["ok"] is True
    assert not source.exists()
    assert (destination / source.name).read_bytes() == b"verified bytes" * 1024
    assert len(journal["operations"][0]["sha256"]) == 64
    assert not list(destination.glob(".filesorter-*.partial"))


def test_profile_rules_migrate_read_only_and_are_revisioned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source"
    state_root = tmp_path / "state"
    target.mkdir()
    (target / "music").mkdir()
    legacy_path = tmp_path / "legacy-rules.json"
    legacy_text = '[{"keyword": "legacy", "folder": "music"}]\n'
    legacy_path.write_text(legacy_text, encoding="utf-8")
    monkeypatch.setattr(file_sorter, "JSON_RULES_FILE_PATH", legacy_path)
    monkeypatch.setattr(file_sorter, "RULES_FILE_PATH", legacy_path)
    monkeypatch.setattr(file_sorter, "PY_RULES_FILE_PATH", tmp_path / "missing.py")

    migrated = file_sorter.read_custom_rules(target, state_root=state_root)
    rules_path = file_sorter.get_rules_path(target, state_root=state_root)
    initial_document = json.loads(rules_path.read_text(encoding="utf-8"))
    file_sorter.add_keywords(
        target,
        ["fresh"],
        "music",
        state_root=state_root,
    )
    updated_document = json.loads(rules_path.read_text(encoding="utf-8"))

    assert migrated == [
        file_sorter.KeywordRule(keyword="legacy", folder="music")
    ]
    assert legacy_path.read_text(encoding="utf-8") == legacy_text
    assert initial_document["revision"] == 0
    assert updated_document["revision"] == 1
    assert {item["keyword"] for item in updated_document["rules"]} == {
        "legacy",
        "fresh",
    }


def test_legacy_absolute_rules_only_migrate_when_destination_is_direct_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source"
    target.mkdir()
    direct_child = target / "music"
    direct_child.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    state_root = tmp_path / "state"
    legacy_path = tmp_path / "legacy-rules.json"
    legacy_document = [
        {"keyword": "relative", "folder": "music"},
        {"keyword": "convert", "folder": str(direct_child)},
        {"keyword": "outside", "folder": str(external)},
        {"keyword": "missing", "folder": "not-created"},
        {"keyword": "traversal", "folder": ".."},
    ]
    legacy_path.write_text(
        json.dumps(legacy_document, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(file_sorter, "JSON_RULES_FILE_PATH", legacy_path)
    monkeypatch.setattr(file_sorter, "RULES_FILE_PATH", legacy_path)
    monkeypatch.setattr(file_sorter, "PY_RULES_FILE_PATH", tmp_path / "missing.py")

    migrated = file_sorter.read_custom_rules(target, state_root=state_root)
    profile_path = file_sorter.get_rules_path(target, state_root=state_root)
    profile_document = json.loads(profile_path.read_text(encoding="utf-8"))
    quarantines = list(
        profile_path.parent.glob(
            f"{file_sorter.LEGACY_RULES_QUARANTINE_PREFIX}*.json"
        )
    )

    assert migrated == [
        file_sorter.KeywordRule(keyword="relative", folder="music"),
        file_sorter.KeywordRule(keyword="convert", folder="music"),
    ]
    assert profile_document["enabled"] is False
    assert profile_document["revision"] == 1
    assert profile_document["migration_required_review"] is True
    assert profile_document["migration_rejected_rule_count"] == 3
    assert len(quarantines) == 1
    quarantine = json.loads(quarantines[0].read_text(encoding="utf-8"))
    assert quarantine["rejected_rule_count"] == 3
    assert {
        item["reason"]
        for item in quarantine["rejected_rules"]
    } == {
        "absolute-destination-outside-target",
        "destination-is-not-an-existing-safe-direct-child",
        "destination-is-not-a-direct-child-name",
    }
    assert json.loads(legacy_path.read_text(encoding="utf-8")) == legacy_document

    pending = sorter_v2.load_profile(target, state_root=state_root)
    assert pending.migration_required_review is True
    assert pending.migration_rejected_rule_count == 3
    with pytest.raises(
        sorter_v2.SorterV2Error,
        match="must be reviewed",
    ):
        sorter_v2.save_profile(pending, enabled=True)

    enabled = file_sorter.configure_profile_enabled(
        target,
        True,
        state_root=state_root,
    )
    assert enabled.enabled is True
    assert enabled.migration_required_review is False
    assert enabled.migration_rejected_rule_count == 0
    assert quarantines[0].exists()


def test_external_legacy_destination_is_rejected_without_filesystem_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = (tmp_path / "source").resolve()
    target.mkdir()
    external = (tmp_path / "never-probe").absolute()
    concrete_path = type(external)
    real_resolve = concrete_path.resolve
    real_lstat = concrete_path.lstat

    def guarded_resolve(path: Path, *args, **kwargs):
        if os.path.normcase(str(path)) == os.path.normcase(str(external)):
            raise AssertionError("external destination was resolved")
        return real_resolve(path, *args, **kwargs)

    def guarded_lstat(path: Path, *args, **kwargs):
        if os.path.normcase(str(path)) == os.path.normcase(str(external)):
            raise AssertionError("external destination was inspected")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(concrete_path, "resolve", guarded_resolve)
    monkeypatch.setattr(concrete_path, "lstat", guarded_lstat)

    migration = file_sorter._migrate_legacy_rule_values(
        [{"keyword": "outside", "folder": str(external)}],
        target,
        require_existing_destination=True,
    )

    assert migration.rules == ()
    assert migration.rejected[0]["reason"] == (
        "absolute-destination-outside-target"
    )


def test_upgrade_inbox_migrates_authenticated_rules_once_and_keeps_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source"
    target.mkdir()
    (target / "music").mkdir()
    external = tmp_path / "external"
    external.mkdir()
    state_root = tmp_path / "state"
    packaged_rules = tmp_path / "packaged-rules.json"
    packaged_rules.write_text("[]\n", encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    payload = json.dumps(
        [
            {"keyword": "safe", "folder": "music"},
            {"keyword": "external", "folder": str(external)},
        ],
        ensure_ascii=False,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    inbox_rule = inbox / f"{digest}-keyword_rules.json"
    inbox_rule.write_bytes(payload)
    (inbox / f"{digest}-keyword_rules.py").write_bytes(payload)
    (inbox / f"{'0' * 64}-.file-sorter-rules.json").write_bytes(payload)
    monkeypatch.setattr(
        file_sorter,
        "LEGACY_RULES_MIGRATION_INBOX_DIR",
        inbox,
    )
    monkeypatch.setattr(file_sorter, "JSON_RULES_FILE_PATH", packaged_rules)
    monkeypatch.setattr(file_sorter, "RULES_FILE_PATH", packaged_rules)
    monkeypatch.setattr(file_sorter, "PY_RULES_FILE_PATH", tmp_path / "missing.py")

    migrated = file_sorter.read_custom_rules(target, state_root=state_root)
    profile_path = file_sorter.get_rules_path(target, state_root=state_root)
    profile_document = json.loads(profile_path.read_text(encoding="utf-8"))
    quarantines = list(
        profile_path.parent.glob(
            f"{file_sorter.LEGACY_RULES_QUARANTINE_PREFIX}*.json"
        )
    )

    assert migrated == [
        file_sorter.KeywordRule(keyword="safe", folder="music")
    ]
    assert profile_document["enabled"] is False
    assert profile_document["revision"] == 1
    assert profile_document["migration_required_review"] is True
    assert profile_document["migration_rejected_rule_count"] == 1
    assert profile_document["migrated_from"] == str(inbox_rule.resolve())
    assert len(quarantines) == 1
    assert json.loads(quarantines[0].read_text(encoding="utf-8"))[
        "rejected_rule_count"
    ] == 1


def test_unreadable_authenticated_inbox_rules_do_not_block_profile_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source"
    target.mkdir()
    state_root = tmp_path / "state"
    packaged_rules = tmp_path / "packaged-rules.json"
    packaged_rules.write_text("[]\n", encoding="utf-8")
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    payload = b"{"
    digest = hashlib.sha256(payload).hexdigest()
    (inbox / f"{digest}-keyword_rules.json").write_bytes(payload)
    monkeypatch.setattr(
        file_sorter,
        "LEGACY_RULES_MIGRATION_INBOX_DIR",
        inbox,
    )
    monkeypatch.setattr(file_sorter, "JSON_RULES_FILE_PATH", packaged_rules)
    monkeypatch.setattr(file_sorter, "RULES_FILE_PATH", packaged_rules)

    assert file_sorter.read_custom_rules(target, state_root=state_root) == []
    profile_path = file_sorter.get_rules_path(target, state_root=state_root)
    profile_document = json.loads(profile_path.read_text(encoding="utf-8"))

    assert profile_document["enabled"] is False
    assert profile_document["migration_required_review"] is True
    assert profile_document["migration_rejected_rule_count"] == 1
    assert len(
        list(
            profile_path.parent.glob(
                f"{file_sorter.LEGACY_RULES_QUARANTINE_PREFIX}*.json"
            )
        )
    ) == 1


def test_automation_quarantines_existing_external_profile_and_disables_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "source"
    target.mkdir()
    direct_child = target / "music"
    direct_child.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    state_root = tmp_path / "state"
    configured_path = sorter_v2.profile_path(target, state_root=state_root)
    configured_path.parent.mkdir(parents=True)
    profile_id = configured_path.parent.name
    original_document = {
        "schema_version": 2,
        "profile_id": profile_id,
        "profile_name": None,
        "target": str(target.resolve()),
        "target_dir": str(target.resolve()),
        "revision": 7,
        "enabled": True,
        "quiet_seconds": 0,
        "include": ["*.mp3"],
        "exclude": ["private*"],
        "rules": [
            {"keyword": "local", "folder": "music"},
            {"keyword": "convert", "folder": str(direct_child)},
            {"keyword": "outside", "folder": str(external)},
        ],
        "migrated_from": None,
    }
    configured_path.write_text(
        json.dumps(original_document, ensure_ascii=False),
        encoding="utf-8",
    )

    reports = file_sorter.run_enabled_profiles_once(state_root=state_root)
    migrated_document = json.loads(configured_path.read_text(encoding="utf-8"))
    quarantines = list(
        configured_path.parent.glob(
            f"{file_sorter.LEGACY_RULES_QUARANTINE_PREFIX}*-profile.json"
        )
    )

    assert len(reports) == 1
    assert reports[0]["ok"] is True
    assert reports[0]["migration_required_review"] is True
    assert migrated_document["enabled"] is False
    assert migrated_document["revision"] == 8
    assert migrated_document["migration_required_review"] is True
    assert migrated_document["include"] == ["*.mp3"]
    assert migrated_document["exclude"] == ["private*"]
    assert migrated_document["rules"] == [
        {"keyword": "local", "folder": "music"},
        {"keyword": "convert", "folder": "music"},
    ]
    assert len(quarantines) == 1
    assert json.loads(quarantines[0].read_text(encoding="utf-8")) == original_document
    follow_up = file_sorter.run_enabled_profiles_once(state_root=state_root)
    assert len(follow_up) == 1
    assert follow_up[0]["migration_required_review"] is True
    assert len(
        list(
            configured_path.parent.glob(
                f"{file_sorter.LEGACY_RULES_QUARANTINE_PREFIX}*-profile.json"
            )
        )
    ) == 1


def test_automation_rejects_forged_profile_identity_before_target_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    configured_path = state_root / "profiles" / "forged" / "profile.json"
    configured_path.parent.mkdir(parents=True)
    forged_target = (tmp_path / "must-not-be-accessed").absolute()
    original_document = {
        "schema_version": 2,
        "profile_id": "forged",
        "profile_name": None,
        "target": str(forged_target),
        "target_dir": str(forged_target),
        "revision": 3,
        "enabled": True,
        "quiet_seconds": 0,
        "include": ["*"],
        "exclude": [],
        "rules": [{"keyword": "unsafe", "folder": str(tmp_path)}],
        "migrated_from": None,
    }
    configured_path.write_text(
        json.dumps(original_document),
        encoding="utf-8",
    )

    def forbidden_target_access(_target: str | Path) -> Path:
        raise AssertionError("forged profile target was accessed")

    monkeypatch.setattr(file_sorter, "resolve_target_dir", forbidden_target_access)

    reports = file_sorter._migrate_legacy_profiles_for_automation(state_root)

    assert len(reports) == 1
    assert reports[0]["ok"] is False
    assert reports[0]["migration_required_review"] is True
    assert reports[0]["errors"] == [
        "Profile identity is invalid; target was not accessed."
    ]
    assert json.loads(configured_path.read_text(encoding="utf-8")) == (
        original_document
    )


def test_profile_revision_rejects_stale_concurrent_update(tmp_path: Path) -> None:
    target = tmp_path / "source"
    target.mkdir()
    state_root = tmp_path / "state"
    original = sorter_v2.load_profile(target, state_root=state_root)
    updated = sorter_v2.save_profile(
        original,
        rules=[{"keyword": "one", "folder": "folder"}],
    )

    assert updated.revision == original.revision + 1
    with pytest.raises(sorter_v2.RuleConflictError):
        sorter_v2.save_profile(
            original,
            rules=[{"keyword": "two", "folder": "folder"}],
        )


def test_profile_ids_do_not_collide_after_name_sanitization(tmp_path: Path) -> None:
    target = tmp_path / "source"
    target.mkdir()

    spaced = sorter_v2.profile_id_for(target, "a b")
    dashed = sorter_v2.profile_id_for(target, "a-b")

    assert spaced != dashed


def test_named_profile_with_hyphens_runs_without_id_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source"
    state_root = tmp_path / "state"
    target.mkdir()
    destination = target / "music"
    destination.mkdir()
    legacy_path = tmp_path / "empty-legacy.json"
    legacy_path.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(file_sorter, "JSON_RULES_FILE_PATH", legacy_path)
    monkeypatch.setattr(file_sorter, "RULES_FILE_PATH", legacy_path)
    snapshot = sorter_v2.load_profile(
        target,
        state_root=state_root,
        profile="daily-media-v2",
    )
    snapshot = sorter_v2.save_profile(
        snapshot,
        enabled=True,
        quiet_seconds=0,
        include=["*.mp3"],
        exclude=["private*"],
    )
    included = target / "music-song.mp3"
    included.write_text("included", encoding="utf-8")
    (target / "music-song.txt").write_text("wrong type", encoding="utf-8")
    (target / "private-music.mp3").write_text("excluded", encoding="utf-8")

    first_reports = file_sorter.run_enabled_profiles_once(state_root=state_root)
    reports = file_sorter.run_enabled_profiles_once(state_root=state_root)

    assert snapshot.profile_name == "daily-media-v2"
    assert first_reports[0]["moved_count"] == 0
    assert first_reports[0]["waiting_for_second_observation_count"] == 1
    assert reports[0]["ok"] is True
    assert reports[0]["profile_id"] == snapshot.profile_id
    assert reports[0]["moved_count"] == 1
    assert (destination / included.name).exists()
    assert (target / "music-song.txt").exists()
    assert (target / "private-music.mp3").exists()


def test_background_observation_gate_resets_when_file_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source"
    state_root = tmp_path / "state"
    destination = target / "music"
    destination.mkdir(parents=True)
    legacy_path = tmp_path / "empty-legacy.json"
    legacy_path.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(file_sorter, "JSON_RULES_FILE_PATH", legacy_path)
    monkeypatch.setattr(file_sorter, "RULES_FILE_PATH", legacy_path)
    snapshot = sorter_v2.load_profile(
        target,
        state_root=state_root,
        profile="background-gate",
    )
    sorter_v2.save_profile(
        snapshot,
        enabled=True,
        quiet_seconds=0,
        rules=[{"keyword": "music", "folder": "music"}],
    )
    source = target / "music-track.bin"
    source.write_bytes(b"first")

    first = file_sorter.run_enabled_profiles_once(state_root=state_root)
    original_mtime = source.stat().st_mtime_ns
    source.write_bytes(b"other")
    os.utime(source, ns=(original_mtime + 1_000_000, original_mtime + 1_000_000))
    second = file_sorter.run_enabled_profiles_once(state_root=state_root)
    third = file_sorter.run_enabled_profiles_once(state_root=state_root)

    assert first[0]["waiting_for_second_observation_count"] == 1
    assert second[0]["waiting_for_second_observation_count"] == 1
    assert second[0]["moved_count"] == 0
    assert third[0]["moved_count"] == 1
    assert (destination / source.name).read_bytes() == b"other"


def test_execute_plan_holds_target_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    destination = tmp_path / "music"
    destination.mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_bytes(b"payload")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
    )
    entered = threading.Event()
    release = threading.Event()
    original_move = sorter_v2._same_volume_move

    def delayed_move(*args: object, **kwargs: object) -> str:
        entered.set()
        assert release.wait(timeout=5)
        return original_move(*args, **kwargs)

    monkeypatch.setattr(sorter_v2, "_same_volume_move", delayed_move)
    first_result: list[dict[str, object]] = []

    thread = threading.Thread(
        target=lambda: first_result.append(
            sorter_v2.execute_plan(plan, state_root=state_root)
        )
    )
    thread.start()
    assert entered.wait(timeout=5)
    with pytest.raises(sorter_v2.SorterV2Error, match="target lock"):
        sorter_v2.execute_plan(
            plan,
            state_root=tmp_path / "different-state",
            lock_timeout_seconds=0.05,
        )
    release.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert first_result[0]["ok"] is True


def test_target_lock_is_identical_across_different_state_roots(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()

    first = sorter_v2._target_lock_path(
        target,
        state_root=tmp_path / "state-a",
    )
    second = sorter_v2._target_lock_path(
        target,
        state_root=tmp_path / "state-b",
    )

    assert first == second
    assert first == target


def test_target_lock_is_cross_process_without_writing_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    module_path = Path(sorter_v2.__file__).resolve()
    script = r"""
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("sorter_target_lock_child", sys.argv[1])
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
with module._TargetDirectoryLock(Path(sys.argv[2]), timeout_seconds=1):
    print("ready", flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(module_path), str(target)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(sorter_v2.SorterV2Error, match="target lock"):
            with sorter_v2._TargetDirectoryLock(
                target,
                timeout_seconds=0.05,
            ):
                pass
    finally:
        output, errors = process.communicate(input="\n", timeout=10)

    assert process.returncode == 0, f"{output}\n{errors}"
    assert list(target.iterdir()) == []


def test_failed_publication_validation_preserves_foreign_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    destination_dir = tmp_path / "music"
    destination_dir.mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_bytes(b"original source")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
    )
    destination = destination_dir / source.name

    monkeypatch.setattr(sorter_v2, "same_volume", lambda *_args: False)

    def publish_replaced(_stage: Path, target: Path) -> None:
        target.write_bytes(b"foreign replacement")

    monkeypatch.setattr(sorter_v2, "_publish_staging", publish_replaced)
    result = sorter_v2.execute_plan(plan, state_root=state_root)

    assert result["ok"] is False
    assert source.read_bytes() == b"original source"
    assert destination.read_bytes() == b"foreign replacement"
    assert "preserved for review" in result["errors"][0]


def test_exclusive_file_lock_is_cross_process_and_persistent(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "cross-process.lock"
    module_path = Path(sorter_v2.__file__).resolve()
    script = """
import importlib.util
import sys
from pathlib import Path

spec = importlib.util.spec_from_file_location("sorter_v2_lock_child", sys.argv[1])
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
with module._ExclusiveFileLock(Path(sys.argv[2]), timeout_seconds=1):
    print("ready", flush=True)
    sys.stdin.readline()
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(module_path), str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        with pytest.raises(sorter_v2.SorterV2Error, match="state lock"):
            with sorter_v2._ExclusiveFileLock(
                lock_path,
                timeout_seconds=0.05,
                stale_seconds=0,
            ):
                pass
    finally:
        output, errors = process.communicate(input="\n", timeout=10)

    assert process.returncode == 0, f"{output}\n{errors}"
    assert lock_path.exists()
    with sorter_v2._ExclusiveFileLock(
        lock_path,
        timeout_seconds=0.1,
        stale_seconds=0,
    ):
        pass
    assert lock_path.exists()


def test_pre_execute_validation_runs_inside_target_lock(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    destination = tmp_path / "music"
    destination.mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_bytes(b"payload")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
    )
    validation_ran = False

    def validate() -> None:
        nonlocal validation_ran
        validation_ran = True
        with pytest.raises(sorter_v2.SorterV2Error, match="target lock"):
            with sorter_v2._TargetDirectoryLock(
                tmp_path,
                timeout_seconds=0,
            ):
                pass

    result = sorter_v2.execute_plan(
        plan,
        state_root=state_root,
        pre_execute_validate=validate,
    )

    assert validation_ran is True
    assert result["ok"] is True


def test_execute_plan_rejects_tampered_destination_outside_rule(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    (tmp_path / "music").mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_bytes(b"stay safe")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
        persist=False,
    )
    escaped = tmp_path.parent / f"{tmp_path.name}-escaped.bin"
    plan.operations[0].destination = str(escaped)

    result = sorter_v2.execute_plan(plan, state_root=state_root)

    assert result["ok"] is False
    assert source.read_bytes() == b"stay safe"
    assert not escaped.exists()


def test_apply_rejects_plan_rule_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "source"
    target.mkdir()
    (target / "music").mkdir()
    external = tmp_path / "external"
    external.mkdir()
    source = target / "music-track.mp3"
    source.write_bytes(b"stay in place")
    state_root = tmp_path / "state"
    legacy_path = tmp_path / "legacy.json"
    legacy_path.write_text("[]\n", encoding="utf-8")
    monkeypatch.setattr(file_sorter, "JSON_RULES_FILE_PATH", legacy_path)
    monkeypatch.setattr(file_sorter, "RULES_FILE_PATH", legacy_path)
    monkeypatch.setattr(file_sorter, "PY_RULES_FILE_PATH", tmp_path / "missing.py")
    plan = file_sorter.preview_organize_files(
        target,
        quiet_seconds=0,
        state_root=state_root,
    )
    plan_path = state_root / "plans" / f"{plan.plan_id}.json"
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    document["operations"][0]["folder"] = str(external)
    document["operations"][0]["destination"] = str(external / source.name)
    plan_path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(
        file_sorter.FileSorterError,
        match="destination rule in the plan is no longer valid",
    ):
        file_sorter.apply_organize_plan(
            plan.plan_id,
            target_dir=target,
            state_root=state_root,
        )

    assert source.read_bytes() == b"stay in place"
    assert not (external / source.name).exists()


def test_recovery_rejects_unowned_staging_path(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    journals = state_root / "journals"
    journals.mkdir(parents=True)
    target = tmp_path / "source"
    destination_dir = tmp_path / "destination"
    target.mkdir()
    destination_dir.mkdir()
    source = target / "track.bin"
    source.write_bytes(b"source")
    victim = tmp_path / "must-not-delete.txt"
    victim.write_bytes(b"keep")
    transaction_id = "12345678-1234-1234-1234-123456789abc"
    operation_id = "abcdef12-1234-1234-1234-123456789abc"
    journal_path = journals / f"{transaction_id}.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "transaction_id": transaction_id,
                "plan_id": "87654321-1234-1234-1234-123456789abc",
                "profile_id": "target-test",
                "target_dir": str(target.resolve()),
                "status": "in_progress",
                "operations": [
                    {
                        "operation_id": operation_id,
                        "source": str(source),
                        "destination": str(destination_dir / source.name),
                        "keyword": "track",
                        "folder": str(destination_dir),
                        "source_size": source.stat().st_size,
                        "source_mtime_ns": source.stat().st_mtime_ns,
                        "status": "copying",
                        "staging": str(victim),
                        "sha256": None,
                    }
                ],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    recovered = sorter_v2.recover_transactions(state_root=state_root)

    assert recovered[0]["status"] == "completed_with_errors"
    assert recovered[0]["errors"]
    assert victim.read_bytes() == b"keep"
    assert source.read_bytes() == b"source"


def test_recovery_obeys_target_lock(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    journals = state_root / "journals"
    journals.mkdir(parents=True)
    target = tmp_path / "source"
    target.mkdir()
    transaction_id = "12345678-1234-1234-1234-123456789abc"
    journal_path = journals / f"{transaction_id}.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "transaction_id": transaction_id,
                "plan_id": "87654321-1234-1234-1234-123456789abc",
                "profile_id": "target-test",
                "target_dir": str(target.resolve()),
                "status": "in_progress",
                "operations": [],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )
    with sorter_v2._TargetDirectoryLock(target):
        recovered = sorter_v2.recover_transactions(
            state_root=state_root,
            lock_timeout_seconds=0.01,
        )

    assert recovered[0]["status"] == "busy"
    assert json.loads(journal_path.read_text(encoding="utf-8"))["status"] == "in_progress"


def test_undo_rejects_journal_source_outside_target(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    journals = state_root / "journals"
    journals.mkdir(parents=True)
    target = tmp_path / "source"
    moved_dir = tmp_path / "destination"
    target.mkdir()
    moved_dir.mkdir()
    moved = moved_dir / "track.bin"
    moved.write_bytes(b"moved payload")
    outside_original = tmp_path / "outside.bin"
    transaction_id = "12345678-1234-1234-1234-123456789abc"
    journal_path = journals / f"{transaction_id}.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "transaction_id": transaction_id,
                "plan_id": "87654321-1234-1234-1234-123456789abc",
                "profile_id": "target-test",
                "target_dir": str(target.resolve()),
                "status": "committed",
                "operations": [
                    {
                        "operation_id": "abcdef12-1234-1234-1234-123456789abc",
                        "source": str(outside_original),
                        "destination": str(moved),
                        "keyword": "track",
                        "folder": str(moved_dir),
                        "source_size": moved.stat().st_size,
                        "source_mtime_ns": moved.stat().st_mtime_ns,
                        "status": "committed",
                        "staging": None,
                        "sha256": sorter_v2.sha256_file(moved),
                    }
                ],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    undone = sorter_v2.undo_transaction(
        transaction_id,
        state_root=state_root,
    )

    assert undone["ok"] is False
    assert moved.read_bytes() == b"moved payload"
    assert not outside_original.exists()


def test_recovery_finishes_undo_interrupted_after_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    destination = tmp_path / "music"
    destination.mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_bytes(b"undo crash payload")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
    )
    applied = file_sorter.apply_organize_plan(
        plan.plan_id,
        target_dir=tmp_path,
        state_root=state_root,
    )
    moved = destination / source.name
    original_undo = sorter_v2._move_exact_for_undo

    def crash_after_move(*args: object, **kwargs: object) -> None:
        original_undo(*args, **kwargs)
        raise SystemExit("simulated process termination")

    monkeypatch.setattr(sorter_v2, "_move_exact_for_undo", crash_after_move)
    with pytest.raises(SystemExit):
        sorter_v2.undo_transaction(
            applied["transaction_id"],
            state_root=state_root,
        )
    journal_path = Path(applied["journal_path"])
    interrupted = json.loads(journal_path.read_text(encoding="utf-8"))
    assert interrupted["status"] == "undo_in_progress"
    assert interrupted["operations"][0]["status"] == "undoing"
    assert source.read_bytes() == b"undo crash payload"
    assert not moved.exists()

    monkeypatch.setattr(sorter_v2, "_move_exact_for_undo", original_undo)
    recovered = sorter_v2.recover_transactions(state_root=state_root)
    finished = json.loads(journal_path.read_text(encoding="utf-8"))

    assert recovered[0]["status"] == "undone"
    assert recovered[0]["recovery_kind"] == "undo"
    assert finished["status"] == "undone"
    assert finished["operations"][0]["status"] == "undone"
    assert source.read_bytes() == b"undo crash payload"
    assert not moved.exists()


def test_recovery_preserves_both_copies_without_durable_undo_proof(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    destination = tmp_path / "music"
    destination.mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_bytes(b"two verified copies")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
    )
    applied = file_sorter.apply_organize_plan(
        plan.plan_id,
        target_dir=tmp_path,
        state_root=state_root,
    )
    moved = destination / source.name
    source.write_bytes(moved.read_bytes())
    journal_path = Path(applied["journal_path"])
    interrupted = json.loads(journal_path.read_text(encoding="utf-8"))
    interrupted["status"] = "undo_in_progress"
    interrupted["operations"][0]["status"] = "undoing"
    journal_path.write_text(json.dumps(interrupted), encoding="utf-8")

    recovered = sorter_v2.recover_transactions(state_root=state_root)
    finished = json.loads(journal_path.read_text(encoding="utf-8"))

    assert recovered[0]["status"] == "undo_failed"
    assert source.read_bytes() == b"two verified copies"
    assert moved.read_bytes() == b"two verified copies"
    assert finished["operations"][0]["status"] == "undo_failed"
    assert "both copies were preserved" in (
        finished["operations"][0]["undo_error"].lower()
    )


def test_recovery_deletes_only_hardlink_with_durable_undo_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_root = tmp_path / "state"
    destination = tmp_path / "music"
    destination.mkdir()
    source = tmp_path / "music-track.mp3"
    source.write_bytes(b"durably proven undo")
    plan = file_sorter.preview_organize_files(
        tmp_path,
        quiet_seconds=0,
        state_root=state_root,
    )
    applied = file_sorter.apply_organize_plan(
        plan.plan_id,
        target_dir=tmp_path,
        state_root=state_root,
    )
    moved = destination / source.name
    original_undo = sorter_v2._move_exact_for_undo

    def crash_after_publication(
        moved_path: Path,
        original_path: Path,
        *,
        expected_hash: str,
        on_published: object,
    ) -> None:
        metadata = sorter_v2._capture_file_metadata(moved_path)
        os.link(moved_path, original_path)
        assert os.path.samefile(moved_path, original_path)
        on_published("hardlink", metadata)  # type: ignore[operator]
        raise SystemExit("simulated termination after durable undo proof")

    monkeypatch.setattr(sorter_v2, "_move_exact_for_undo", crash_after_publication)
    with pytest.raises(SystemExit):
        sorter_v2.undo_transaction(
            applied["transaction_id"],
            state_root=state_root,
        )
    journal_path = Path(applied["journal_path"])
    interrupted = json.loads(journal_path.read_text(encoding="utf-8"))
    assert interrupted["operations"][0]["undo_publication_confirmed"] is True
    assert source.read_bytes() == b"durably proven undo"
    assert moved.read_bytes() == b"durably proven undo"
    assert os.path.samefile(source, moved)

    monkeypatch.setattr(sorter_v2, "_move_exact_for_undo", original_undo)
    recovered = sorter_v2.recover_transactions(state_root=state_root)
    finished = json.loads(journal_path.read_text(encoding="utf-8"))

    assert recovered[0]["status"] == "undone"
    assert source.read_bytes() == b"durably proven undo"
    assert not moved.exists()
    assert finished["operations"][0]["status"] == "undone"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows process query")
def test_windows_process_probe_is_non_destructive() -> None:
    assert sorter_v2._windows_process_alive(os.getpid()) is True


def test_cli_preview_apply_history_and_profiles_are_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_root = tmp_path / "state"
    (tmp_path / "music").mkdir()
    (tmp_path / "music-track.mp3").write_text("move", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "file-sorter",
            str(tmp_path),
            "--preview-json",
            "--quiet-seconds",
            "0",
            "--state-root",
            str(state_root),
        ],
    )
    assert file_sorter.main() == 0
    preview = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "file-sorter",
            str(tmp_path),
            "--apply-plan",
            preview["plan_id"],
            "--state-root",
            str(state_root),
        ],
    )
    assert file_sorter.main() == 0
    applied = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "file-sorter",
            str(tmp_path),
            "--history-json",
            "--state-root",
            str(state_root),
        ],
    )
    assert file_sorter.main() == 0
    history = json.loads(capsys.readouterr().out)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "file-sorter",
            str(tmp_path),
            "--profiles-json",
            "--state-root",
            str(state_root),
        ],
    )
    assert file_sorter.main() == 0
    profiles = json.loads(capsys.readouterr().out)

    assert preview["type"] == "file-sorter-plan"
    assert applied["plan_id"] == preview["plan_id"]
    assert history["history"][0]["transaction_id"] == applied["transaction_id"]
    assert profiles["type"] == "file-sorter-profiles"


def test_recovery_finishes_verified_publication(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    journals = state_root / "journals"
    journals.mkdir(parents=True)
    target = tmp_path / "source"
    destination_dir = target / "destination"
    target.mkdir()
    destination_dir.mkdir()
    source = target / "track.bin"
    destination = destination_dir / source.name
    source.write_bytes(b"same verified content")
    destination.write_bytes(source.read_bytes())
    digest = sorter_v2.sha256_file(source)
    transaction_id = "12345678-1234-1234-1234-123456789abc"
    journal_path = journals / f"{transaction_id}.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "transaction_id": transaction_id,
                "plan_id": "87654321-1234-1234-1234-123456789abc",
                "profile_id": "target-test",
                "target_dir": str(target.resolve()),
                "status": "in_progress",
                "operations": [
                    {
                        "source": str(source),
                        "destination": str(destination),
                        "folder": destination_dir.name,
                        "status": "published",
                        "staging": None,
                        "sha256": digest,
                    }
                ],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    recovered = sorter_v2.recover_transactions(
        state_root=state_root,
        target_dir=target,
    )
    updated = json.loads(journal_path.read_text(encoding="utf-8"))

    assert recovered[0]["recovered_count"] == 1
    assert not source.exists()
    assert destination.read_bytes() == b"same verified content"
    assert updated["status"] == "committed"


def test_recovery_preserves_source_without_durable_publication_proof(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    journals = state_root / "journals"
    journals.mkdir(parents=True)
    target = tmp_path / "source"
    destination_dir = target / "destination"
    target.mkdir()
    destination_dir.mkdir()
    source = target / "track.bin"
    destination = destination_dir / source.name
    source.write_bytes(b"same content is not ownership proof")
    destination.write_bytes(source.read_bytes())
    digest = sorter_v2.sha256_file(source)
    transaction_id = "22345678-1234-1234-1234-123456789abc"
    journal_path = journals / f"{transaction_id}.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "transaction_id": transaction_id,
                "plan_id": "97654321-1234-1234-1234-123456789abc",
                "profile_id": "target-test",
                "target_dir": str(target.resolve()),
                "status": "in_progress",
                "operations": [
                    {
                        "operation_id": "32345678-1234-1234-1234-123456789abc",
                        "source": str(source),
                        "destination": str(destination),
                        "folder": destination_dir.name,
                        "status": "publishing",
                        "publication_confirmed": False,
                        "staging": None,
                        "sha256": digest,
                    }
                ],
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    recovered = sorter_v2.recover_transactions(
        state_root=state_root,
        target_dir=target,
    )
    updated = json.loads(journal_path.read_text(encoding="utf-8"))

    assert recovered[0]["recovered_count"] == 0
    assert recovered[0]["status"] == "completed_with_errors"
    assert source.read_bytes() == b"same content is not ownership proof"
    assert destination.read_bytes() == source.read_bytes()
    assert updated["operations"][0]["status"] == "recovery_failed"
