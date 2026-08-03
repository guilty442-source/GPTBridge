from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CLEANER_SRC = ROOT / "global-cleaner" / "src"
if str(CLEANER_SRC) not in sys.path:
    sys.path.insert(0, str(CLEANER_SRC))

from backend.services.project_cleaner.infrastructure.cleanup_engine import ProjectCleanupService  # noqa: E402
from backend.services.project_cleaner.infrastructure.cleanup_engine import SYSTEM_RESCUE_REQUIRED_PATHS  # noqa: E402


def test_global_cleanup_targets_exact_legacy_paths_and_prunes_packages(
    tmp_path: Path,
) -> None:
    obsolete = tmp_path / "ai-assistant" / "data" / "business"
    active_runtime = tmp_path / "ai-assistant" / "runtime"
    unrelated = tmp_path / "other-tool" / "runtime"
    packaged = tmp_path / "other-tool" / "dist"
    dependency = tmp_path / "node_modules" / "dependency"
    for directory in (obsolete, active_runtime, unrelated, packaged, dependency):
        directory.mkdir(parents=True)
        (directory / "payload.bin").write_bytes(b"x")

    service = ProjectCleanupService(tmp_path)
    plan = service.plan_cleanup("global")
    paths = {str(item["path"]) for item in plan["items"]}

    assert "ai-assistant/data/business" in paths
    assert "ai-assistant/runtime" not in paths
    assert "other-tool/runtime" not in paths
    assert all(not path.startswith("other-tool/dist") for path in paths)
    assert all(not path.startswith("node_modules") for path in paths)
    assert service.runtime_root == (
        tmp_path / "global-cleaner" / "runtime" / "state" / "cleanup"
    )


def test_storage_analysis_includes_every_project_directory(
    tmp_path: Path,
) -> None:
    payloads = {
        ".git/objects/one.bin": b"git-data",
        ".venv/Lib/site-packages/two.bin": b"venv-data",
        "tool/node_modules/package/three.bin": b"dependency-data",
        "tool/dist/four.bin": b"distribution-data",
        "global-cleaner/data/business/backups/owner/five.zip": b"backup-data",
        "global-cleaner/runtime/quarantine/batch/items/six.bin": b"recovery-data",
    }
    for relative, content in payloads.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    progress: list[dict[str, object]] = []
    result = ProjectCleanupService(
        tmp_path,
        progress_callback=progress.append,
    ).analyze_storage("global")

    assert result["ok"] is True
    assert result["file_count"] == len(payloads)
    assert result["total_bytes"] == sum(
        len(content) for content in payloads.values()
    )
    assert any(event.get("phase") == "analyze-scan" for event in progress)
    assert progress[-1]["percent"] == 100


def test_confirmed_cleanup_permanently_deletes_verified_low_risk_item(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "tool" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"generated")
    service = ProjectCleanupService(tmp_path)
    plan = service.plan_cleanup("global")

    denied = service.cleanup_garbage(
        "global",
        plan_id=str(plan["plan_id"]),
        plan_token=str(plan["plan_token"]),
    )
    assert denied["error_code"] == "DIRECT_DELETE_CONFIRMATION_REQUIRED"
    assert cache.exists()

    result = service.cleanup_garbage(
        "global",
        plan_id=str(plan["plan_id"]),
        plan_token=str(plan["plan_token"]),
        confirm_direct_delete=True,
    )
    assert result["ok"] is True
    assert result["permanently_deleted"] == 1
    assert result["disk_space_reclaimed_bytes"] > 0
    assert cache.is_dir()
    assert not any(cache.iterdir())


def test_locked_descendant_is_detected_before_directory_cleanup(
    tmp_path: Path,
    monkeypatch,
) -> None:
    candidate = tmp_path / "Cache"
    locked = candidate / "nested" / "journal.baj"
    locked.parent.mkdir(parents=True)
    locked.write_bytes(b"locked")
    service = ProjectCleanupService(tmp_path)
    monkeypatch.setattr(service, "_is_locked", lambda path: path == locked)

    assert service._locked_descendant(candidate) == locked


def test_central_managed_temp_root_is_a_cleanup_candidate(
    tmp_path: Path,
) -> None:
    managed_temp = tmp_path / "global-cleaner" / "runtime" / "temp"
    tool_temp = managed_temp / "tools" / "sample-tool"
    shared_temp = managed_temp / "shared-layer"
    tool_temp.mkdir(parents=True)
    shared_temp.mkdir(parents=True)
    nested_temp = tool_temp / "nested" / "retained"
    nested_temp.mkdir(parents=True)
    (nested_temp / "one.tmp").write_text("temporary", encoding="utf-8")
    (shared_temp / "two.tmp").write_text("temporary", encoding="utf-8")

    service = ProjectCleanupService(tmp_path)
    plan = service.plan_cleanup("global")
    paths = {item["path"] for item in plan["items"]}

    assert "global-cleaner/runtime/temp" in paths

    result = service.cleanup_garbage(
        "global",
        plan_id=str(plan["plan_id"]),
        plan_token=str(plan["plan_token"]),
        confirm_direct_delete=True,
    )
    assert result["ok"] is True
    assert tool_temp.is_dir()
    assert nested_temp.is_dir()
    assert not any(path.is_file() for path in tool_temp.rglob("*"))
    assert shared_temp.is_dir() and not any(shared_temp.iterdir())


def test_obsolete_cleaner_history_file_is_targeted_without_targeting_its_folder(
    tmp_path: Path,
) -> None:
    business = tmp_path / "global-cleaner" / "data" / "business"
    business.mkdir(parents=True)
    history = business / "history.sqlite3"
    history.write_bytes(b"obsolete")

    service = ProjectCleanupService(tmp_path)
    plan = service.plan_cleanup("global")
    items = {str(item["path"]): item for item in plan["items"]}

    assert items["global-cleaner/data/business/history.sqlite3"]["type"] == "file"
    assert "global-cleaner/data/business" not in items


def test_known_legacy_data_is_targeted_without_removing_owner_folders(
    tmp_path: Path,
) -> None:
    legacy_files = (
        "shared-layer/data/shared-layer.sqlite3",
        "vaultly/data/business/vaultly.sqlite3",
        "runtime/global-cleaner/plan.json",
        "main-system/src-core/edge-profile/Cache/data.bin",
    )
    for relative in legacy_files:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"legacy")

    service = ProjectCleanupService(tmp_path)
    plan = service.plan_cleanup("global")
    items = {str(item["path"]): item for item in plan["items"]}

    assert "shared-layer/data/shared-layer.sqlite3" in items
    assert "vaultly/data/business/vaultly.sqlite3" in items
    assert items["runtime/global-cleaner"]["contents_only"] is True
    assert items["main-system/src-core/edge-profile"]["contents_only"] is True


def test_quarantine_moves_only_temp_files_and_restore_merges_them_back(
    tmp_path: Path,
) -> None:
    temp_root = (
        tmp_path / "global-cleaner" / "runtime" / "temp" / "tools" / "sample-tool"
    )
    nested = temp_root / "nested" / "retained"
    nested.mkdir(parents=True)
    temporary_file = nested / "payload.tmp"
    temporary_file.write_bytes(b"temporary")

    service = ProjectCleanupService(tmp_path)
    plan = service.plan_cleanup("global")
    item = next(
        item
        for item in plan["items"]
        if item["path"] == "global-cleaner/runtime/temp"
    )
    result = service.cleanup_garbage(
        "global",
        quarantine=True,
        plan_id=str(plan["plan_id"]),
        plan_token=str(plan["plan_token"]),
        selected_item_ids=[str(item["item_id"])],
    )

    assert result["ok"] is True
    assert result["cleaned_files"] == 1
    assert result["cleaned_dirs"] == 0
    assert nested.is_dir()
    assert not temporary_file.exists()

    restored = service.restore_quarantine(Path(result["quarantine_path"]).name)

    assert restored["ok"] is True
    assert restored["restored"] == 1
    assert nested.is_dir()
    assert temporary_file.read_bytes() == b"temporary"


def test_system_health_uses_current_main_system_layout() -> None:
    assert "main-system/src-core/main.py" in SYSTEM_RESCUE_REQUIRED_PATHS
    assert (
        "system-rescue/src/backend/services/system_rescue/integration/"
        "platform_packager.py"
    ) in SYSTEM_RESCUE_REQUIRED_PATHS
    assert "main-system/scripts/package_platform_tools.py" not in (
        SYSTEM_RESCUE_REQUIRED_PATHS
    )


def test_system_health_treats_explicitly_deferred_packaging_as_warning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    script = (
        tmp_path
        / "system-rescue"
        / "src"
        / "backend"
        / "services"
        / "system_rescue"
        / "integration"
        / "platform_packager.py"
    )
    script.parent.mkdir(parents=True)
    script.write_text("# verification entry", encoding="utf-8")
    service = ProjectCleanupService(tmp_path)
    payload = {
        "ok": False,
        "results": [
            {"tool_id": "file-sorter", "error_code": "STALE_PACKAGE"},
            {"tool_id": "governance_rule", "error_code": "PACKAGE_MISSING"},
        ],
    }
    monkeypatch.setattr(
        service,
        "_system_rescue_subprocess",
        lambda *_args, **_kwargs: {
            "ok": False,
            "exit_code": 1,
            "output": __import__("json").dumps(payload),
        },
    )

    result = service._system_rescue_package_check()

    assert result["ok"] is True
    assert result["packaging_deferred"] is True
    assert result["blocking_results"] == []


def test_force_purge_permanently_deletes_verified_quarantine(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "tool" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"generated")
    service = ProjectCleanupService(tmp_path)
    plan = service.plan_cleanup("global")
    quarantined = service.cleanup_garbage(
        "global",
        quarantine=True,
        plan_id=str(plan["plan_id"]),
        plan_token=str(plan["plan_token"]),
    )
    assert quarantined["ok"] is True

    result = service.purge_quarantine(permanent=True)

    assert result["ok"] is True
    assert result["permanently_deleted"] == 1
    assert result["disk_space_reclaimed_bytes"] > 0
    assert not service.quarantine_root.exists() or not any(
        service.quarantine_root.iterdir()
    )


def test_legacy_purge_preserves_nested_business_backup_and_recovery_data(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    release.mkdir()
    (release / "obsolete.bin").write_bytes(b"obsolete")
    business_recovery = tmp_path / "vaultly" / "data" / "business" / "recovery"
    business_recovery.mkdir(parents=True)
    recovery_file = business_recovery / "account.json"
    recovery_file.write_text("owner data", encoding="utf-8")
    backup_copy = tmp_path / "vaultly" / "data" / "business" / "account.bak"
    backup_copy.write_text("owner backup", encoding="utf-8")

    result = ProjectCleanupService(tmp_path).purge_legacy_artifacts(force=True)

    assert result["ok"] is True
    assert not release.exists()
    assert recovery_file.read_text(encoding="utf-8") == "owner data"
    assert backup_copy.read_text(encoding="utf-8") == "owner backup"
