import importlib.util
import json
import os
import sys
import time
from pathlib import Path


sys.path.insert(0, os.path.join(os.getcwd(), "src-core"))

ROOT = Path(__file__).resolve().parents[1]
CLEANUP_SERVICE_PATH = (
    ROOT
    / "platform_tools"
    / "project-cleaner"
    / "src"
    / "backend"
    / "cleanup_service.py"
)
SPEC = importlib.util.spec_from_file_location(
    "project_cleaner_cleanup_service",
    CLEANUP_SERVICE_PATH,
)
assert SPEC and SPEC.loader
cleanup_service_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cleanup_service_module
SPEC.loader.exec_module(cleanup_service_module)
ProjectCleanupService = cleanup_service_module.ProjectCleanupService


def age_file(path: Path, days: int) -> None:
    timestamp = time.time() - (days * 24 * 60 * 60)
    os.utime(path, (timestamp, timestamp))


def test_project_cleanup_skips_dependency_and_profile_dirs(tmp_path: Path) -> None:
    dependency_cache = (
        tmp_path
        / "release"
        / "win-unpacked"
        / "resources"
        / ".venv"
        / "cache"
    )
    dependency_cache.mkdir(parents=True)
    dependency_file = dependency_cache / "keep.tmp"
    dependency_file.write_text("keep", encoding="utf-8")

    profile_cache = tmp_path / "runtime" / "profiles" / "main" / "cache"
    profile_cache.mkdir(parents=True)
    profile_file = profile_cache / "keep.tmp"
    profile_file.write_text("keep", encoding="utf-8")

    log_dir = tmp_path / "release" / "logs"
    log_dir.mkdir(parents=True)
    old_log = log_dir / "delete.log"
    old_log.write_text("delete", encoding="utf-8")
    age_file(old_log, 8)
    recent_log = log_dir / "keep.log"
    recent_log.write_text("keep", encoding="utf-8")

    service = ProjectCleanupService(tmp_path)
    result = service.cleanup_garbage("project", dry_run=False)

    assert result["ok"] is True
    assert result["cleaned_files"] >= 1
    assert dependency_file.exists()
    assert profile_file.exists()
    assert not old_log.exists()
    assert recent_log.exists()
    assert log_dir.exists()


def test_global_cleanup_scans_whole_project_but_skips_protected_dirs(
    tmp_path: Path,
) -> None:
    pycache_dir = tmp_path / "platform_tools" / "project-cleaner" / "src" / "__pycache__"
    pycache_dir.mkdir(parents=True)
    pycache_file = pycache_dir / "main.pyc"
    pycache_file.write_bytes(b"delete")

    src_cache = tmp_path / "src-core" / "tasks" / "cache"
    src_cache.mkdir(parents=True)
    src_cache_file = src_cache / "work.tmp"
    src_cache_file.write_text("delete", encoding="utf-8")

    source_file = tmp_path / "src-core" / "tasks" / "service.py"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("keep", encoding="utf-8")

    dependency_cache = tmp_path / "node_modules" / "package" / "cache"
    dependency_cache.mkdir(parents=True)
    dependency_file = dependency_cache / "keep.tmp"
    dependency_file.write_text("keep", encoding="utf-8")

    profile_cache = tmp_path / "edge-profile" / "main" / "cache"
    profile_cache.mkdir(parents=True)
    profile_file = profile_cache / "keep.tmp"
    profile_file.write_text("keep", encoding="utf-8")

    service = ProjectCleanupService(tmp_path)
    result = service.cleanup_garbage("global", dry_run=False)

    assert result["ok"] is True
    assert result["scope"] == "global"
    assert not pycache_dir.exists()
    assert not src_cache.exists()
    assert source_file.exists()
    assert dependency_file.exists()
    assert profile_file.exists()


def test_cleanup_dry_run_returns_plan_without_removing_files(tmp_path: Path) -> None:
    cache_dir = tmp_path / "runtime" / "cache"
    cache_dir.mkdir(parents=True)
    cache_file = cache_dir / "work.tmp"
    cache_file.write_text("delete", encoding="utf-8")

    service = ProjectCleanupService(tmp_path)
    result = service.cleanup_garbage("runtime", dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["planned_dirs"] == 1
    assert result["items"][0]["path"] == "runtime/cache"
    assert result["summary"]["by_type"]["directory"]["count"] == 1
    assert result["summary"]["by_risk"]["low"]["size_bytes"] >= 6
    assert result["summary"]["largest_items"][0]["path"] == "runtime/cache"
    assert result["health"]["state"] == "ready"
    assert result["health"]["score"] == 100
    assert result["health"]["recommended_action"] == "quarantine"
    assert result["health"]["direct_delete_allowed"] is True
    assert cache_file.exists()


def test_cleanup_plan_reports_medium_risk_health(tmp_path: Path) -> None:
    backup_file = tmp_path / ".GPTBridge_RuntimeSandbox" / "state.old"
    backup_file.parent.mkdir(parents=True)
    backup_file.write_text("old", encoding="utf-8")
    age_file(backup_file, 20)

    service = ProjectCleanupService(tmp_path)
    plan = service.plan_cleanup("sandbox")

    assert plan["ok"] is True
    assert plan["health"]["state"] == "review"
    assert plan["health"]["safety_level"] == "medium"
    assert plan["health"]["requires_review"] is True
    assert plan["health"]["direct_delete_allowed"] is False


def test_cleanup_quarantine_can_be_restored(tmp_path: Path) -> None:
    tmp_file = tmp_path / "runtime" / "old.tmp"
    tmp_file.parent.mkdir(parents=True)
    tmp_file.write_text("delete", encoding="utf-8")
    age_file(tmp_file, 2)

    service = ProjectCleanupService(tmp_path)
    result = service.cleanup_garbage("runtime", dry_run=False, quarantine=True)

    assert result["ok"] is True
    assert result["cleaned_files"] == 1
    assert not tmp_file.exists()
    assert result["quarantine_path"]
    manifest = Path(result["quarantine_manifest"])
    assert manifest.exists()

    batches = service.list_quarantine_batches()

    assert batches["ok"] is True
    assert batches["batch_count"] == 1
    assert batches["batches"][0]["name"] == Path(result["quarantine_path"]).name
    assert batches["batches"][0]["item_count"] == 1
    assert batches["batches"][0]["size_bytes"] >= len("delete")

    restore_result = service.restore_quarantine(Path(result["quarantine_path"]).name)

    assert restore_result["ok"] is True
    assert restore_result["restored"] == 1
    assert tmp_file.exists()
    assert tmp_file.read_text(encoding="utf-8") == "delete"


def test_cleanup_purges_old_quarantine_batches(tmp_path: Path) -> None:
    old_batch = tmp_path / ".GPTBridge_CleanerQuarantine" / "old"
    old_batch.mkdir(parents=True)
    (old_batch / "item.tmp").write_text("delete", encoding="utf-8")
    old_timestamp = time.time() - (48 * 60 * 60)
    os.utime(old_batch / "item.tmp", (old_timestamp, old_timestamp))
    os.utime(old_batch, (old_timestamp, old_timestamp))

    service = ProjectCleanupService(tmp_path)
    batches = service.list_quarantine_batches()

    assert batches["health"]["state"] == "attention"
    assert batches["health"]["expired_count"] == 1
    assert batches["batches"][0]["expired"] is True

    result = service.purge_quarantine(older_than_hours=24)

    assert result["ok"] is True
    assert result["purged_dirs"] == 1
    assert not old_batch.exists()


def test_cleanup_status_reports_version_and_quarantine_health(tmp_path: Path) -> None:
    service = ProjectCleanupService(tmp_path)
    status = service.get_status()

    assert status["ok"] is True
    assert status["version"] == "1.2.0"
    assert status["default_scope"] == "runtime"
    assert status["quarantine_health"]["state"] == "empty"
    assert "runtime" in status["supported_scopes"]


def test_cleanup_rules_live_in_project_cleaner_backend() -> None:
    core_source = (ROOT / "src-core" / "settings" / "service.py").read_text(
        encoding="utf-8"
    )
    ipc_source = (ROOT / "src-core" / "ipc" / "server.py").read_text(
        encoding="utf-8"
    )
    cleaner_source = CLEANUP_SERVICE_PATH.read_text(encoding="utf-8")
    ui_source = (
        ROOT
        / "platform_tools"
        / "project-cleaner"
        / "src"
        / "ui"
        / "ProjectCleanerWindowApp.tsx"
    ).read_text(encoding="utf-8")
    manifest = json.loads(
        (ROOT / "platform_tools" / "project-cleaner" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert "settings_cleanup_garbage" not in core_source
    assert "settings_cleanup_garbage" not in ipc_source
    assert "ProjectCleanupService" not in core_source
    assert "removable_names" not in core_source
    assert "removable_suffixes" not in core_source
    assert "ProjectCleanupService" in cleaner_source
    assert "plan_cleanup" in cleaner_source
    assert "get_status" in cleaner_source
    assert "project-cleaner__health" in ui_source
    assert "--status" in ui_source
    assert manifest["version"] == "1.2.0"
