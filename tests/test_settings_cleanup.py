import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


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


def apply_cleanup(
    service: ProjectCleanupService,
    scope: str,
    *,
    quarantine: bool = False,
    selected_item_ids: list[str] | None = None,
) -> dict:
    plan = service.cleanup_garbage(scope, dry_run=True)
    assert plan["ok"] is True
    return service.cleanup_garbage(
        scope,
        quarantine=quarantine,
        plan_id=plan["plan_id"],
        plan_token=plan["plan_token"],
        selected_item_ids=selected_item_ids,
        confirm_direct_delete=not quarantine,
    )


def test_legacy_purge_is_project_bound_and_preserves_current_packages(
    tmp_path: Path,
) -> None:
    registered_tool = tmp_path / "platform_tools" / "active-tool"
    current_package = registered_tool / "dist" / "active-tool.exe"
    package_backup = registered_tool / "build" / "previous-package.zip"
    current_package.parent.mkdir(parents=True)
    current_package.write_bytes(b"current")
    package_backup.parent.mkdir(parents=True)
    package_backup.write_bytes(b"old")
    (registered_tool / "manifest.json").write_text("{}", encoding="utf-8")

    obsolete_tool = tmp_path / "platform_tools" / "obsolete-tool" / "dist"
    obsolete_tool.mkdir(parents=True)
    (obsolete_tool / "obsolete.exe").write_bytes(b"obsolete")
    backup_file = tmp_path / "backups" / "old.zip"
    backup_file.parent.mkdir()
    backup_file.write_bytes(b"backup")
    release_file = tmp_path / "release" / "failed-installer.exe"
    release_file.parent.mkdir()
    release_file.write_bytes(b"failed")
    profile_backup = tmp_path / "edge-profile" / "state.old"
    profile_backup.parent.mkdir()
    profile_backup.write_bytes(b"profile")

    service = ProjectCleanupService(tmp_path)
    denied = service.purge_legacy_artifacts()
    result = service.purge_legacy_artifacts(force=True)

    assert denied["error_code"] == "CONFIRMATION_REQUIRED"
    assert result["ok"] is True
    assert result["boundary"] == "project-only"
    assert result["permanently_deleted"] is True
    assert current_package.exists()
    assert not package_backup.exists()
    assert not obsolete_tool.parent.exists()
    assert not backup_file.exists()
    assert not release_file.exists()
    assert profile_backup.exists()


def test_project_cleanup_skips_active_output_dependency_and_profile_dirs(
    tmp_path: Path,
) -> None:
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
    result = apply_cleanup(service, "project")

    assert result["ok"] is True
    assert result["cleaned_files"] == 0
    assert dependency_file.exists()
    assert profile_file.exists()
    assert old_log.exists()
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

    packaged_cache = (
        tmp_path
        / "platform_tools"
        / "demo"
        / "dist"
        / "resources"
        / "app"
        / "src-core"
        / "__pycache__"
    )
    packaged_cache.mkdir(parents=True)
    packaged_file = packaged_cache / "keep.pyc"
    packaged_file.write_bytes(b"current package")

    rollback_cache = (
        tmp_path
        / "platform_tools"
        / "demo"
        / "build"
        / "package-rollback"
        / "dist"
        / "__pycache__"
    )
    rollback_cache.mkdir(parents=True)
    rollback_file = rollback_cache / "keep.pyc"
    rollback_file.write_bytes(b"rollback evidence")

    service = ProjectCleanupService(tmp_path)
    result = apply_cleanup(service, "global")

    assert result["ok"] is True
    assert result["scope"] == "global"
    assert not pycache_dir.exists()
    assert not src_cache.exists()
    assert source_file.exists()
    assert dependency_file.exists()
    assert profile_file.exists()
    assert packaged_file.exists()
    assert rollback_file.exists()


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


def test_cleanup_quarantines_owned_visual_smoke_sandbox_as_one_item(
    tmp_path: Path,
) -> None:
    sandbox = (
        tmp_path
        / ".GPTBridge_RuntimeSandbox"
        / "gptbridge-ai-assistant-visual-file-sorter-ABC123"
    )
    (sandbox / "LocalAppData" / "runtime").mkdir(parents=True)
    (sandbox / "LocalAppData" / "runtime" / "state.bin").write_bytes(b"generated")

    service = ProjectCleanupService(tmp_path)
    sandbox_plan = service.plan_cleanup("sandbox")
    global_plan = service.plan_cleanup("global")

    assert [item["path"] for item in sandbox_plan["items"]] == [
        ".GPTBridge_RuntimeSandbox/gptbridge-ai-assistant-visual-file-sorter-ABC123"
    ]
    assert sandbox_plan["items"][0]["rule_id"] == "visual-smoke-sandbox"
    assert not any(
        item["rule_id"] == "visual-smoke-sandbox" for item in global_plan["items"]
    )


def test_anomaly_repair_can_be_bounded_to_upgrade_relevant_types(
    tmp_path: Path,
) -> None:
    stale_plan = tmp_path / "runtime" / "project-cleaner" / "plans" / "stale.json"
    stale_plan.parent.mkdir(parents=True)
    stale_plan.write_text("not-json", encoding="utf-8")
    completed_recovery = (
        tmp_path
        / "platform_tools"
        / "demo"
        / "build"
        / "package-old"
    )
    completed_recovery.mkdir(parents=True)
    (completed_recovery / "promotion-complete.json").write_text(
        '{"status":"complete-with-recovery"}',
        encoding="utf-8",
    )
    (completed_recovery / "promotion-recovery-manifest.json").write_text(
        '{"status":"retained"}',
        encoding="utf-8",
    )
    newer_recovery = completed_recovery.parent / "package-new"
    newer_recovery.mkdir()
    (newer_recovery / "promotion-complete.json").write_text(
        '{"status":"complete-with-recovery"}',
        encoding="utf-8",
    )
    (newer_recovery / "promotion-recovery-manifest.json").write_text(
        '{"status":"retained"}',
        encoding="utf-8",
    )
    os.utime(completed_recovery, (1, 1))
    os.utime(newer_recovery, (2, 2))

    result = ProjectCleanupService(tmp_path).repair_anomalies(
        dry_run=True,
        include_shared=True,
        selected_anomalies={"stale-preview-plan"},
    )

    assert result["repairable_count"] == 1
    assert result["items"][0]["anomaly"] == "stale-preview-plan"
    assert result["selected_anomalies"] == ["stale-preview-plan"]


def test_cleanup_quarantine_can_be_restored(tmp_path: Path) -> None:
    tmp_file = tmp_path / "runtime" / "old.tmp"
    tmp_file.parent.mkdir(parents=True)
    tmp_file.write_text("delete", encoding="utf-8")
    age_file(tmp_file, 2)

    service = ProjectCleanupService(tmp_path)
    result = apply_cleanup(service, "runtime", quarantine=True)

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
    old_file = tmp_path / "runtime" / "old.tmp"
    old_file.parent.mkdir(parents=True)
    old_file.write_text("delete", encoding="utf-8")
    age_file(old_file, 2)
    service = ProjectCleanupService(tmp_path)
    cleanup = apply_cleanup(service, "runtime", quarantine=True)
    old_batch = Path(cleanup["quarantine_path"])
    manifest_path = old_batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_time = datetime.now(timezone.utc) - timedelta(hours=48)
    manifest["created_at"] = old_time.isoformat()
    manifest["expires_at"] = (old_time + timedelta(hours=24)).isoformat()
    service._write_batch_document(old_batch, manifest)
    batches = service.list_quarantine_batches()

    assert batches["health"]["state"] == "attention"
    assert batches["health"]["expired_count"] == 1
    assert batches["batches"][0]["expired"] is True

    result = service.purge_quarantine(older_than_hours=24)

    assert result["ok"] is True
    assert result["purged_dirs"] == 1
    assert not old_batch.exists()


def test_restore_and_purge_same_batch_are_exclusive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime" / "race.tmp"
    target.parent.mkdir(parents=True)
    target.write_text("race-safe", encoding="utf-8")
    age_file(target, 2)
    creator = ProjectCleanupService(tmp_path)
    cleanup = apply_cleanup(creator, "runtime", quarantine=True)
    batch = Path(cleanup["quarantine_path"])

    manifest_path = batch / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    old_time = datetime.now(timezone.utc) - timedelta(hours=48)
    manifest["created_at"] = old_time.isoformat()
    manifest["expires_at"] = (old_time + timedelta(hours=1)).isoformat()
    creator._write_batch_document(batch, manifest)

    restore_started = threading.Event()
    allow_restore = threading.Event()
    original_move = shutil.move

    def blocking_move(source: str, destination: str) -> str:
        restore_started.set()
        assert allow_restore.wait(timeout=5)
        return original_move(source, destination)

    monkeypatch.setattr(shutil, "move", blocking_move)
    restore_service = ProjectCleanupService(tmp_path)
    purge_service = ProjectCleanupService(tmp_path)
    restore_result: dict[str, object] = {}

    def run_restore() -> None:
        restore_result.update(restore_service.restore_quarantine(batch.name))

    worker = threading.Thread(target=run_restore, daemon=True)
    worker.start()
    assert restore_started.wait(timeout=5)

    purge_result = purge_service.purge_quarantine(older_than_hours=1)

    assert purge_result["ok"] is False
    assert purge_result["busy"] is True
    assert purge_result["purged_dirs"] == 0
    assert batch.exists()

    allow_restore.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert restore_result["ok"] is True
    assert restore_result["restored"] == 1
    assert target.read_text(encoding="utf-8") == "race-safe"


def test_operation_lock_is_contained_and_persistent(tmp_path: Path) -> None:
    service = ProjectCleanupService(tmp_path)
    lock_path = service._operation_lock_path("../../outside")
    expected_root = (service.runtime_root / "locks").resolve()

    assert lock_path.parent == expected_root
    assert lock_path.suffix == ".lock"
    with service._mutation_guard("test") as acquired:
        assert acquired["acquired"] is True
        assert lock_path.parent.exists()

    mutation_lock = service._operation_lock_path("project-mutation")
    metadata = json.loads(mutation_lock.read_text(encoding="utf-8"))
    assert metadata["state"] == "released"
    assert metadata["pid"] == os.getpid()
    assert mutation_lock.exists()


def test_mutation_lock_is_exclusive_across_processes(tmp_path: Path) -> None:
    service = ProjectCleanupService(tmp_path)
    child_script = """
import importlib.util
import json
import sys
from pathlib import Path

module_path = Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("project_cleaner_child_service", module_path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
result = module.ProjectCleanupService(Path(sys.argv[2])).purge_quarantine(1)
print(json.dumps(result))
"""

    with service._mutation_guard("parent-test") as acquired:
        assert acquired["acquired"] is True
        child = subprocess.run(
            [sys.executable, "-c", child_script, str(CLEANUP_SERVICE_PATH), str(tmp_path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )

    result = json.loads(child.stdout)
    assert result["ok"] is False
    assert result["busy"] is True
    assert result["purged_dirs"] == 0


def test_cleanup_status_reports_version_and_quarantine_health(tmp_path: Path) -> None:
    service = ProjectCleanupService(tmp_path)
    status = service.get_status()

    assert status["ok"] is True
    assert status["version"] == "1.0.0"
    assert status["default_scope"] == "runtime"
    assert status["quarantine_health"]["state"] == "empty"
    assert "runtime" in status["supported_scopes"]


def _seed_system_rescue_project(project_root: Path) -> None:
    for relative_path in (
        "src-core/main.py",
        "src-core/ipc/server.py",
        "src-core/ipc/handlers.py",
        "scripts/package_platform_tools.py",
    ):
        target = project_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# rescue fixture\n", encoding="utf-8")
    for relative_path in (
        "package.json",
        "config/settings.json",
        "config/tool-runtime-contract.json",
    ):
        target = project_root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}\n", encoding="utf-8")


def test_system_rescue_check_is_owned_by_cleaner_and_project_bounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_system_rescue_project(tmp_path)
    service = ProjectCleanupService(tmp_path)
    monkeypatch.setattr(
        service,
        "_system_rescue_package_check",
        lambda: {"ok": True, "results": [], "message": "verified"},
    )
    monkeypatch.setattr(
        service,
        "_system_rescue_main_health",
        lambda: {"ok": True, "reachable": True, "phase": "full_mode_ready"},
    )

    result = service.system_rescue_check()

    assert result["ok"] is True
    assert result["state"] == "healthy"
    assert result["authority"] == "project-cleaner"
    assert result["boundary"] == "project-only"
    assert result["project_root"] == str(tmp_path.resolve())
    assert result["blocking"] == []


def test_system_rescue_repair_uses_cleaner_quarantine_then_rechecks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_system_rescue_project(tmp_path)
    stale_plan = tmp_path / "runtime" / "project-cleaner" / "plans" / "stale.json"
    stale_plan.parent.mkdir(parents=True)
    stale_plan.write_text("invalid", encoding="utf-8")
    service = ProjectCleanupService(tmp_path)
    monkeypatch.setattr(
        service,
        "_system_rescue_package_check",
        lambda: {"ok": True, "results": [], "message": "verified"},
    )
    monkeypatch.setattr(
        service,
        "_system_rescue_main_health",
        lambda: {"ok": False, "reachable": False, "message": "offline"},
    )

    result = service.system_rescue_repair()

    assert result["ok"] is True
    assert result["authority"] == "project-cleaner"
    assert result["boundary"] == "project-only"
    assert result["repaired_count"] == 1
    assert result["unresolved_count"] == 0
    assert result["quarantine_path"]
    assert not stale_plan.exists()


def test_cleanup_rules_live_in_project_cleaner_backend() -> None:
    core_path = ROOT / "src-core" / "settings" / "service.py"
    core_source = core_path.read_text(encoding="utf-8") if core_path.exists() else ""
    ipc_source = (ROOT / "src-core" / "ipc" / "server.py").read_text(
        encoding="utf-8"
    )
    cleaner_source = CLEANUP_SERVICE_PATH.read_text(encoding="utf-8")
    cleaner_engine_source = (
        CLEANUP_SERVICE_PATH.with_name("cleanup_engine.py")
    ).read_text(encoding="utf-8")
    cleaner_main_source = (
        ROOT / "platform_tools" / "project-cleaner" / "src" / "main.py"
    ).read_text(encoding="utf-8")
    shared_cleanup_source = (
        ROOT / "src-core" / "utils" / "cleanup.py"
    ).read_text(encoding="utf-8")
    toolbox_source = (
        ROOT / "src-core" / "tasks" / "toolbox_service.py"
    ).read_text(encoding="utf-8")
    project_entry_source = (ROOT / "run.py").read_text(encoding="utf-8")
    ui_source = (
        ROOT
        / "platform_tools"
        / "project-cleaner"
        / "src"
        / "ui"
        / "ProjectCleanerWindowApp.tsx"
    ).read_text(encoding="utf-8")
    runner_source = (
        ROOT
        / "platform_tools"
        / "project-cleaner"
        / "src"
        / "ui"
        / "toolWindowRunner.ts"
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
    assert "def repair_anomalies(" in cleaner_engine_source
    assert "def system_rescue_check(" in cleaner_engine_source
    assert "def system_rescue_repair(" in cleaner_engine_source
    assert "GPTBRIDGE_CLEANER_TARGET_ROOT" in cleaner_main_source
    assert "--system-rescue-check" in cleaner_main_source
    assert "--system-rescue-repair" in cleaner_main_source
    assert '"mutation_root"' in cleaner_engine_source
    assert '"outside-project"' in cleaner_engine_source
    assert "def perform_cleanup(" not in shared_cleanup_source
    assert "def run_recoverable_cleanup(" not in project_entry_source
    assert "def run_project_cleaner(" in project_entry_source
    assert not (ROOT / "src-core" / "tasks" / "rescue_service.py").exists()
    assert manifest["capabilities"]["system-rescue"]["operations"]["check"] == [
        "--system-rescue-check",
        "--json",
    ]
    assert (
        manifest["environment"]["bindings"]["GPTBRIDGE_CLEANER_TARGET_ROOT"]
        == "project_root"
    )
    assert 'if tool_id == "project-cleaner"' not in toolbox_source
    assert "project-cleaner__health" in ui_source
    assert "--status" in ui_source
    assert "--plan-id" in ui_source
    assert "--selected-item" in ui_source
    assert "--analyze-storage" in ui_source
    assert "--system-rescue-check" in ui_source
    assert "--system-rescue-repair" in ui_source
    assert "cancelToolRun" in ui_source
    assert "isProjectCleanerReadOnlyRun" in runner_source
    assert "'--repair-anomalies'" in runner_source
    assert "'--system-rescue-repair'" in runner_source
    assert "queued: true" not in runner_source
    assert "後端連線尚未就緒，指令未送出" in runner_source
    assert "String(payload.request_id || '') === targetRequestId" in runner_source
    assert "ACTIVE_PROJECT_CLEANER_REQUEST_IDS" in runner_source
    assert "RUN_CANCELLATION_GRACE_MS" in runner_source
    assert "if (queueId && removeQueuedCommand(queueId))" in runner_source
    assert "cancellationGraceTimer = window.setTimeout" in runner_source
    assert "PROJECT_CLEANER_PROGRESS_JSON=" in (
        ROOT / "src-core" / "tasks" / "toolbox_service.py"
    ).read_text(encoding="utf-8")
    assert manifest["version"] == "1.0.0"


def test_cleanup_apply_requires_valid_signed_preview(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "old.tmp"
    target.parent.mkdir(parents=True)
    target.write_text("keep until verified", encoding="utf-8")
    age_file(target, 2)
    service = ProjectCleanupService(tmp_path)

    missing_plan = service.cleanup_garbage(
        "runtime",
        confirm_direct_delete=True,
    )
    plan = service.cleanup_garbage("runtime", dry_run=True)
    bad_signature = service.cleanup_garbage(
        "runtime",
        plan_id=plan["plan_id"],
        plan_token="0" * 64,
        confirm_direct_delete=True,
    )

    assert missing_plan["ok"] is False
    assert "preview plan" in missing_plan["message"]
    assert bad_signature["ok"] is False
    assert "signature" in bad_signature["message"]
    assert target.exists()


def test_cleanup_rejects_expired_and_changed_plans(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "old.tmp"
    target.parent.mkdir(parents=True)
    target.write_text("before", encoding="utf-8")
    age_file(target, 2)
    service = ProjectCleanupService(tmp_path)
    plan = service.cleanup_garbage("runtime", dry_run=True)

    target.write_text("after preview", encoding="utf-8")
    changed = service.cleanup_garbage(
        "runtime",
        quarantine=True,
        plan_id=plan["plan_id"],
        plan_token=plan["plan_token"],
    )
    assert changed["ok"] is True
    assert changed["cleaned_files"] == 0
    assert changed["skipped"][0]["reason"] == "candidate changed after preview"
    assert target.exists()

    second_plan = service.cleanup_garbage("runtime", dry_run=True)
    plan_path = service.plan_root / f"{second_plan['plan_id']}.json"
    document = json.loads(plan_path.read_text(encoding="utf-8"))
    document["expires_at"] = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    document["plan_token"] = service._plan_signature(document)
    plan_path.write_text(json.dumps(document), encoding="utf-8")
    expired = service.cleanup_garbage(
        "runtime",
        quarantine=True,
        plan_id=second_plan["plan_id"],
        plan_token=document["plan_token"],
    )
    assert expired["ok"] is False
    assert "expired" in expired["message"]


def test_cleanup_protects_git_tracked_candidates(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    target = tmp_path / "runtime" / "old.tmp"
    target.parent.mkdir(parents=True)
    target.write_text("tracked", encoding="utf-8")
    age_file(target, 2)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-f", "runtime/old.tmp"], check=True)

    plan = ProjectCleanupService(tmp_path).plan_cleanup("runtime")

    assert plan["git"]["available"] is True
    assert not any(item["path"] == "runtime/old.tmp" for item in plan["items"])
    assert any(item["reason"] == "git-tracked file" for item in plan["skipped"])


def test_cleanup_selected_items_and_locked_file_are_never_forced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "runtime" / "first.tmp"
    second = tmp_path / "runtime" / "second.tmp"
    first.parent.mkdir(parents=True)
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    age_file(first, 2)
    age_file(second, 2)
    service = ProjectCleanupService(tmp_path)
    plan = service.cleanup_garbage("runtime", dry_run=True)
    first_item = next(item for item in plan["items"] if item["path"] == "runtime/first.tmp")

    selected = service.cleanup_garbage(
        "runtime",
        quarantine=True,
        plan_id=plan["plan_id"],
        plan_token=plan["plan_token"],
        selected_item_ids=[first_item["item_id"]],
    )
    assert selected["cleaned_files"] == 1
    assert not first.exists()
    assert second.exists()

    second_plan = service.cleanup_garbage("runtime", dry_run=True)
    monkeypatch.setattr(service, "_is_locked", lambda _path: True)
    monkeypatch.setattr(
        service,
        "_locking_processes",
        lambda _path: [{"pid": 321, "name": "EXCEL.EXE", "restartable": True}],
    )
    locked = service.cleanup_garbage(
        "runtime",
        quarantine=True,
        plan_id=second_plan["plan_id"],
        plan_token=second_plan["plan_token"],
    )
    assert locked["cleaned_files"] == 0
    assert locked["skipped"][0]["locked_by"][0]["name"] == "EXCEL.EXE"
    assert "never forces unlock" in locked["skipped"][0]["reason"]
    assert second.exists()


def test_transactional_quarantine_recovers_after_post_move_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime" / "recover.tmp"
    target.parent.mkdir(parents=True)
    target.write_text("recover me", encoding="utf-8")
    age_file(target, 2)
    service = ProjectCleanupService(tmp_path)
    plan = service.cleanup_garbage("runtime", dry_run=True)

    def fail_digest(_path: Path, _item_type: str) -> str:
        raise OSError("simulated digest failure")

    monkeypatch.setattr(service, "_content_digest", fail_digest)
    cleanup = service.cleanup_garbage(
        "runtime",
        quarantine=True,
        plan_id=plan["plan_id"],
        plan_token=plan["plan_token"],
    )
    batch_name = Path(cleanup["quarantine_path"]).name
    batches = service.list_quarantine_batches()

    assert cleanup["ok"] is False
    assert not target.exists()
    assert batches["batches"][0]["recoverable"] is True
    assert batches["batches"][0]["restorable_count"] == 1
    restored = service.restore_quarantine(batch_name)
    assert restored["ok"] is True
    assert restored["restored"] == 1
    assert target.read_text(encoding="utf-8") == "recover me"


def test_quarantine_intent_survives_process_crash_after_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "runtime" / "crash.tmp"
    target.parent.mkdir(parents=True)
    target.write_text("survive crash", encoding="utf-8")
    age_file(target, 2)
    service = ProjectCleanupService(tmp_path)
    plan = service.cleanup_garbage("runtime", dry_run=True)
    original_move = shutil.move

    def move_then_crash(source: str, destination: str) -> str:
        result = original_move(source, destination)
        raise SystemExit("simulated process crash")

    monkeypatch.setattr(shutil, "move", move_then_crash)
    with pytest.raises(SystemExit):
        service.cleanup_garbage(
            "runtime",
            quarantine=True,
            plan_id=plan["plan_id"],
            plan_token=plan["plan_token"],
        )
    monkeypatch.setattr(shutil, "move", original_move)

    batch = next(service.quarantine_root.iterdir())
    journal = json.loads((batch / "journal.json").read_text(encoding="utf-8"))
    assert journal["items"][0]["status"] == "moving"
    assert journal["items"][0]["quarantine_path"]
    assert not target.exists()

    restored = service.restore_quarantine(batch.name)
    assert restored["ok"] is True
    assert restored["restored"] == 1
    assert target.read_text(encoding="utf-8") == "survive crash"


def test_atomic_json_write_fsyncs_before_replace_and_parent_afterward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ProjectCleanupService(tmp_path)
    target = tmp_path / "runtime" / "project-cleaner" / "durable.json"
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = os.replace

    def tracking_fsync(descriptor: int) -> None:
        events.append("file-fsync")
        original_fsync(descriptor)

    def tracking_replace(source: str | bytes | Path, destination: str | bytes | Path) -> None:
        events.append("replace")
        original_replace(source, destination)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    monkeypatch.setattr(os, "replace", tracking_replace)
    monkeypatch.setattr(
        service,
        "_fsync_parent_directory",
        lambda _directory: events.append("parent-fsync"),
    )

    service._atomic_write_json(target, {"ok": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}
    assert events == ["file-fsync", "replace", "parent-fsync"]
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_batch_document_falls_back_when_manifest_or_journal_is_invalid(
    tmp_path: Path,
) -> None:
    service = ProjectCleanupService(tmp_path)
    for invalid_name, valid_name in (
        ("manifest.json", "journal.json"),
        ("journal.json", "manifest.json"),
    ):
        batch = service.quarantine_root / invalid_name.removesuffix(".json")
        batch.mkdir(parents=True)
        (batch / invalid_name).write_text("{not-json", encoding="utf-8")
        valid_document = {
            "revision": 7,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "status": valid_name,
            "items": [],
        }
        (batch / valid_name).write_text(
            json.dumps(valid_document),
            encoding="utf-8",
        )

        loaded, error = service._read_batch_document(batch)

        assert error == ""
        assert loaded is not None
        assert loaded["status"] == valid_name
        assert service._batch_document_path(batch) == batch / valid_name


def test_batch_document_prefers_newer_revision_then_updated_at(
    tmp_path: Path,
) -> None:
    service = ProjectCleanupService(tmp_path)
    batch = service.quarantine_root / "newest"
    batch.mkdir(parents=True)
    earlier = datetime.now(timezone.utc) - timedelta(minutes=5)
    later = datetime.now(timezone.utc)
    manifest = {
        "revision": 4,
        "updated_at": later.isoformat(),
        "status": "older-revision",
        "items": [],
    }
    journal = {
        "revision": 5,
        "updated_at": earlier.isoformat(),
        "status": "newer-revision",
        "items": [],
    }
    (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (batch / "journal.json").write_text(json.dumps(journal), encoding="utf-8")

    loaded, error = service._read_batch_document(batch)

    assert error == ""
    assert loaded is not None
    assert loaded["status"] == "newer-revision"

    manifest["revision"] = 5
    manifest["updated_at"] = earlier.isoformat()
    manifest["status"] = "older-timestamp"
    journal["updated_at"] = later.isoformat()
    journal["status"] = "newer-timestamp"
    (batch / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (batch / "journal.json").write_text(json.dumps(journal), encoding="utf-8")

    loaded, error = service._read_batch_document(batch)

    assert error == ""
    assert loaded is not None
    assert loaded["status"] == "newer-timestamp"
    assert service._batch_document_path(batch) == batch / "journal.json"


def test_failed_manifest_write_leaves_newer_journal_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ProjectCleanupService(tmp_path)
    batch = service.quarantine_root / "interrupted"
    batch.mkdir(parents=True)
    document = {
        "status": "completed",
        "items": [],
    }
    service._write_batch_document(batch, document)
    assert document["revision"] == 1

    original_atomic_write = service._atomic_write_json

    def fail_manifest(path: Path, payload: dict[str, object]) -> None:
        if path.name == "manifest.json":
            raise OSError("simulated manifest persistence failure")
        original_atomic_write(path, payload)

    document["status"] = "restored"
    monkeypatch.setattr(service, "_atomic_write_json", fail_manifest)

    with pytest.raises(OSError, match="manifest persistence failure"):
        service._write_batch_document(batch, document)

    manifest = json.loads((batch / "manifest.json").read_text(encoding="utf-8"))
    journal = json.loads((batch / "journal.json").read_text(encoding="utf-8"))
    loaded, error = service._read_batch_document(batch)

    assert manifest["revision"] == 1
    assert manifest["status"] == "completed"
    assert journal["revision"] == 2
    assert journal["status"] == "restored"
    assert error == ""
    assert loaded is not None
    assert loaded["revision"] == 2
    assert loaded["status"] == "restored"


def test_quarantine_integrity_pin_and_rename_conflict(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "old.tmp"
    target.parent.mkdir(parents=True)
    target.write_text("original", encoding="utf-8")
    age_file(target, 2)
    service = ProjectCleanupService(tmp_path)
    cleanup = apply_cleanup(service, "runtime", quarantine=True)
    batch_name = Path(cleanup["quarantine_path"]).name

    pinned = service.set_quarantine_pinned(batch_name, True)
    assert pinned["ok"] is True
    assert service.list_quarantine_batches()["batches"][0]["pinned"] is True
    target.write_text("new destination", encoding="utf-8")
    restored = service.restore_quarantine(batch_name, conflict_strategy="rename")
    renamed_path = tmp_path / "runtime" / "old.restored-1.tmp"
    assert restored["restored"] == 1
    assert restored["renamed"] == 1
    assert target.read_text(encoding="utf-8") == "new destination"
    assert renamed_path.read_text(encoding="utf-8") == "original"

    integrity_target = tmp_path / "runtime" / "integrity.tmp"
    integrity_target.write_text("trusted", encoding="utf-8")
    age_file(integrity_target, 2)
    second = apply_cleanup(service, "runtime", quarantine=True)
    second_manifest = json.loads(Path(second["quarantine_manifest"]).read_text(encoding="utf-8"))
    integrity_item = next(
        item for item in second_manifest["items"] if item["path"] == "runtime/integrity.tmp"
    )
    quarantined = Path(second["quarantine_path"]) / integrity_item["quarantine_path"]
    quarantined.write_text("tampered", encoding="utf-8")
    rejected = service.restore_quarantine(Path(second["quarantine_path"]).name)
    assert any(item["reason"] == "quarantine integrity mismatch" for item in rejected["skipped"])
    assert not integrity_target.exists()


def test_legacy_quarantine_manifest_remains_restorable(tmp_path: Path) -> None:
    batch = tmp_path / ".GPTBridge_CleanerQuarantine" / "legacy"
    quarantined = batch / "runtime" / "legacy.tmp"
    quarantined.parent.mkdir(parents=True)
    quarantined.write_text("legacy", encoding="utf-8")
    (batch / "manifest.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "scope": "runtime",
                "items": [
                    {
                        "original_path": "runtime/legacy.tmp",
                        "quarantine_path": "runtime/legacy.tmp",
                        "type": "file",
                        "size_bytes": 6,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    service = ProjectCleanupService(tmp_path)

    listed = service.list_quarantine_batches()
    restored = service.restore_quarantine("legacy")

    assert listed["batches"][0]["status"] == "legacy"
    assert listed["batches"][0]["restorable_count"] == 1
    assert restored["restored"] == 1
    assert (tmp_path / "runtime" / "legacy.tmp").read_text(encoding="utf-8") == "legacy"


def test_rule_preferences_storage_analysis_and_automatic_cleanup(tmp_path: Path) -> None:
    override_path = tmp_path / "config" / "project-cleaner-rules.json"
    override_path.parent.mkdir(parents=True)
    override_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "file_rules": [
                    {
                        "id": "scratch",
                        "patterns": ["*.scratch"],
                        "reason": "custom scratch file",
                        "risk": "low",
                        "min_age_days": 0,
                    },
                    {
                        "id": "temporary",
                        "patterns": ["*.tmp"],
                        "reason": "temporary file",
                        "risk": "low",
                        "min_age_days": 1,
                    },
                ],
                "analysis": {
                    "duplicate_min_size_bytes": 16,
                    "large_file_min_size_bytes": 16,
                    "large_file_min_age_days": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    scratch = tmp_path / "runtime" / "custom.scratch"
    scratch.parent.mkdir(parents=True)
    scratch.write_text("custom", encoding="utf-8")
    duplicate_a = tmp_path / "data" / "a.bin"
    duplicate_b = tmp_path / "data" / "b.bin"
    duplicate_a.parent.mkdir(parents=True)
    duplicate_a.write_bytes(b"same-content" * 20)
    duplicate_b.write_bytes(b"same-content" * 20)
    auto_target = tmp_path / "runtime" / "auto.tmp"
    auto_target.write_text("auto", encoding="utf-8")
    age_file(auto_target, 2)
    service = ProjectCleanupService(tmp_path)

    preferences = service.update_preferences(
        automation_enabled=True,
        quarantine_ttl_hours=72,
    )
    plan = service.plan_cleanup("runtime")
    analysis = service.analyze_storage("global")
    automatic = service.automatic_cleanup(force=True)
    status = service.get_status()

    assert preferences["automation"]["enabled"] is True
    assert preferences["quarantine_ttl_hours"] == 72
    assert any(item["path"] == "runtime/custom.scratch" for item in plan["items"])
    assert analysis["duplicate_group_count"] == 1
    assert analysis["duplicate_groups"][0]["copies"] == 2
    assert analysis["large_stale_count"] >= 2
    assert automatic["automatic"] is True
    assert automatic["cleaned_files"] >= 1
    assert len(status["history"]) >= 4
    assert status["rules"]["override_exists"] is True


def test_rule_override_cannot_remove_core_protected_paths(tmp_path: Path) -> None:
    override_path = tmp_path / "config" / "project-cleaner-rules.json"
    override_path.parent.mkdir(parents=True)
    override_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "excluded_directory_names": [],
                "protected_relative_paths": [],
            }
        ),
        encoding="utf-8",
    )

    service = ProjectCleanupService(tmp_path)

    excluded = {
        str(item).casefold()
        for item in service.rules["excluded_directory_names"]
    }
    protected = {
        str(item).replace("\\", "/").casefold()
        for item in service.rules["protected_relative_paths"]
    }
    assert "browser-profiles" in excluded
    assert "runtime/browser-profiles" in protected
    assert "runtime/file-sorter" in protected
    assert "runtime/ipc" in protected


def test_invalid_rule_override_is_ignored(tmp_path: Path) -> None:
    override_path = tmp_path / "config" / "project-cleaner-rules.json"
    override_path.parent.mkdir(parents=True)
    override_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "protected_relative_paths": [],
                "file_rules": "unsafe",
            }
        ),
        encoding="utf-8",
    )

    service = ProjectCleanupService(tmp_path)

    assert any("ignored" in warning for warning in service.rule_warnings)
    assert isinstance(service.rules["file_rules"], list)
    assert "runtime/ipc" in service.rules["protected_relative_paths"]


def test_anomaly_repair_is_recoverable_and_project_bounded(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside.tmp"
    outside.write_text("outside must remain", encoding="utf-8")

    invalid_plan = (
        project_root
        / "runtime"
        / "project-cleaner"
        / "plans"
        / f"{'a' * 32}.json"
    )
    invalid_plan.parent.mkdir(parents=True)
    invalid_plan.write_text("{broken", encoding="utf-8")

    build_stamp = (
        project_root
        / "launcher"
        / "state"
        / "production-build.sha256"
    )
    build_stamp.parent.mkdir(parents=True)
    build_stamp.write_text("not-a-signature\n", encoding="ascii")

    build_root = project_root / "platform_tools" / "demo" / "build"
    stale_lock = build_root / ".package-demo.lock"
    stale_lock.mkdir(parents=True)
    (stale_lock / "owner.json").write_text(
        json.dumps({"tool_id": "demo", "pid": 99999999}),
        encoding="utf-8",
    )
    old_timestamp = time.time() - 120
    os.utime(stale_lock, (old_timestamp, old_timestamp))

    latest_recovery = build_root / "package-latest"
    old_recovery = build_root / "package-old"
    incomplete_recovery = build_root / "package-incomplete"
    for package_root in (
        latest_recovery,
        old_recovery,
        incomplete_recovery,
    ):
        package_root.mkdir()
        (package_root / "promotion-complete.json").write_text(
            "{}",
            encoding="utf-8",
        )
        (
            package_root / "promotion-recovery-manifest.json"
        ).write_text("{}", encoding="utf-8")
    (incomplete_recovery / "promotion-aborted.json").write_text(
        json.dumps({"status": "rollback-incomplete"}),
        encoding="utf-8",
    )
    os.utime(old_recovery, (old_timestamp, old_timestamp))

    service = ProjectCleanupService(project_root)
    preview = service.repair_anomalies(dry_run=True)

    preview_paths = {item["path"] for item in preview["items"]}
    assert preview["ok"] is True
    assert preview["repairable_count"] == 4
    assert "runtime/project-cleaner/plans/" + ("a" * 32) + ".json" in preview_paths
    assert "launcher/state/production-build.sha256" in preview_paths
    assert "platform_tools/demo/build/.package-demo.lock" in preview_paths
    assert "platform_tools/demo/build/package-old" in preview_paths
    assert outside.exists()
    assert invalid_plan.exists()
    assert incomplete_recovery.exists()

    repaired = service.repair_anomalies()

    assert repaired["ok"] is True
    assert repaired["repaired_count"] == 4
    assert repaired["permanently_deleted"] == 0
    assert not invalid_plan.exists()
    assert not build_stamp.exists()
    assert not stale_lock.exists()
    assert not old_recovery.exists()
    assert latest_recovery.exists()
    assert incomplete_recovery.exists()
    assert outside.read_text(encoding="utf-8") == "outside must remain"

    restored = service.restore_quarantine(
        Path(repaired["quarantine_path"]).name,
    )
    assert restored["ok"] is True
    assert restored["restored"] == 4
    assert invalid_plan.exists()
    assert build_stamp.exists()
    assert stale_lock.exists()
    assert old_recovery.exists()


def test_cleaner_capability_contract_classifies_and_bounds_features(
    tmp_path: Path,
) -> None:
    service = ProjectCleanupService(tmp_path)

    status = service.get_status()
    capabilities = status["capabilities"]

    assert capabilities["mutation_root"] == str(tmp_path.resolve())
    assert "anomaly-diagnosis" in capabilities["read_only"]
    assert "anomaly-repair" in capabilities["recoverable_mutation"]
    assert capabilities["maintenance_layers"] == [
        "cleaner",
        "shared",
        "package",
    ]
    assert {
        "outside-project",
        "source-code-edit",
        "git-tracked-file",
        "force-unlock",
        "process-termination",
        "active-package-lock",
        "current-dist",
        "rollback-incomplete",
        "dependency-directory",
        "browser-profile",
        "user-data",
    }.issubset(set(capabilities["forbidden"]))


def test_cleanup_cli_emits_progress_and_final_json(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "old.tmp"
    target.parent.mkdir(parents=True)
    target.write_text("preview", encoding="utf-8")
    age_file(target, 2)
    env = dict(os.environ)
    env["GPTBRIDGE_PROJECT_ROOT"] = str(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "platform_tools" / "project-cleaner" / "src" / "main.py"),
            "--cleanup-garbage",
            "--scope",
            "runtime",
            "--dry-run",
            "--progress-jsonl",
            "--json",
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    lines = completed.stdout.splitlines()

    assert completed.returncode == 0, completed.stderr
    assert any(line.startswith("PROJECT_CLEANER_PROGRESS_JSON=") for line in lines)
    payload = json.loads(lines[-1])
    assert payload["ok"] is True
    assert payload["plan_id"]
    assert payload["health"]["cleanliness_score"] < 100
