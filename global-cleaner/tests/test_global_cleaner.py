"""global-cleaner consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "global-cleaner" / "src"),
    str(_ROOT / "ai-assistant" / "src"),
    str(_ROOT / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

########################################################################
# source: global-cleaner/tests/test_global_cleaner_layering.py
########################################################################
import ast
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "global-cleaner"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "project_cleaner"

from backend.services.project_cleaner.infrastructure.cleanup_engine import (
    ProjectCleanupService,
)


def test_global_cleaner_has_owned_layers_and_thin_entries() -> None:
    for layer in ("application", "domain", "infrastructure"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]
    assert len((TOOL_ROOT / "src" / "main.py").read_text(encoding="utf-8").splitlines()) <= 10
    assert len((TOOL_ROOT / "src" / "test_runner.py").read_text(encoding="utf-8").splitlines()) <= 10


def test_test_artifacts_are_owned_by_global_cleaner() -> None:
    manifest = json.loads((TOOL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    artifacts = manifest["capabilities"]["global-cleanup"]["test_artifacts"]
    sandbox = (
        TOOL_ROOT
        / "src"
        / "backend"
        / "services"
        / "project_cleaner"
        / "infrastructure"
        / "test_sandbox.py"
    ).read_text(encoding="utf-8")

    assert artifacts["owner"] == "global-cleaner"
    assert artifacts["storage"].startswith(
        "global-cleaner/runtime/temp/development/test-artifacts/"
    )
    assert 'tool_root / "runtime" / "temp" / "development"' in sandbox
    assert 'environment["PYTHONPYCACHEPREFIX"]' in sandbox
    assert 'environment["PYTEST_ADDOPTS"]' in sandbox


def test_global_cleaner_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_global_cleaner_database_is_tool_owned(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "global-cleaner").mkdir(parents=True)
    service = ProjectCleanupService(project)
    assert service.business_history.database_path.resolve().is_relative_to(
        (project / "global-cleaner").resolve()
    )


def test_global_cleaner_history_releases_database_handle(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / "global-cleaner").mkdir(parents=True)
    service = ProjectCleanupService(project)

    service._append_history("test", ok=True)
    assert len(service._history_records()) == 1
    database = service.business_history.database_path
    database.unlink()

    assert not database.exists()



########################################################################
# source: global-cleaner/tests/test_global_cleaner_precision.py
########################################################################
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



########################################################################
# source: global-cleaner/tests/test_governed_backup_and_repair.py
########################################################################
import asyncio
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
CLEANER_SRC = ROOT / "global-cleaner" / "src"
MAIN_CORE = ROOT / "main-system" / "src-core"
for source_root in (CLEANER_SRC, MAIN_CORE):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))


from backend.services.project_cleaner.infrastructure.cleanup_engine import ProjectCleanupService  # noqa: E402
from core_system.daily_global_cleaner_service import (  # noqa: E402
    DailyGlobalCleanerService,
)


def _fake_project(root: Path) -> None:
    (root / "main-system").mkdir(parents=True)
    (root / "main-system" / "package.json").write_text(
        json.dumps({"version": "1.0.0"}), encoding="utf-8"
    )
    (root / "main-system" / "src.txt").write_text("main", encoding="utf-8")
    (root / "alpha" / "runtime" / "state").mkdir(parents=True)
    (root / "alpha" / "manifest.json").write_text(
        json.dumps({"id": "alpha", "version": "1.0.0"}), encoding="utf-8"
    )
    (root / "alpha" / "runtime" / "state" / "data.txt").write_text(
        "alpha", encoding="utf-8"
    )


def test_companion_tools_use_host_backup_owner_and_global_cleaner_storage(
    tmp_path: Path,
) -> None:
    _fake_project(tmp_path)
    (tmp_path / "mobile-interface").mkdir()
    (tmp_path / "mobile-interface" / "manifest.json").write_text(
        json.dumps(
            {
                "id": "mobile-interface",
                "version": "1.0.0",
                "backup_owner": "alpha",
                "backup_storage": "global-cleaner/data/business/backups/alpha",
            }
        ),
        encoding="utf-8",
    )
    service = ProjectCleanupService(tmp_path)

    owners = service._registered_backup_owners()

    assert "alpha" in owners
    assert "mobile-interface" not in owners
    assert service.backup_root == (
        tmp_path / "global-cleaner" / "data" / "business" / "backups"
    )


def test_backup_is_per_owner_and_old_generation_is_removed_only_after_verify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_project(tmp_path)
    service = ProjectCleanupService(tmp_path)
    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/global-cleaner",
    )

    first = service.create_managed_backup("alpha")
    assert first["ok"] is True
    assert first["backup_count"] == 1

    original_verify = service._verify_backup_archive
    monkeypatch.setattr(
        service,
        "_verify_backup_archive",
        lambda _path: (False, 0, "forced-verification-failure"),
    )
    failed = service.create_managed_backup("alpha")
    assert failed["ok"] is False
    owner_root = tmp_path / "global-cleaner" / "data" / "business" / "backups" / "alpha"
    assert len(tuple(owner_root.glob("*.zip"))) == 1

    monkeypatch.setattr(service, "_verify_backup_archive", original_verify)
    second = service.create_managed_backup("alpha")
    assert second["ok"] is True
    assert second["backup_count"] == 1
    assert second["removed_excess"] == [Path(first["backup"]).name]


def test_repair_extract_requires_governed_system_rescue_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_project(tmp_path)
    service = ProjectCleanupService(tmp_path)
    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/global-cleaner",
    )
    assert service.create_managed_backup("alpha")["ok"] is True

    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/system-rescue",
    )
    monkeypatch.setenv("GPTBRIDGE_GOVERNED_REQUEST_ID", "repair-alpha-1")
    extracted = service.extract_managed_backup(
        "alpha",
        ["alpha/runtime/state/data.txt"],
    )
    assert extracted["ok"] is True
    assert extracted["apply_authority"] is False
    assert extracted["requester_actor"] == "governance/tool/system-rescue"
    assert len(extracted["items"]) == 1

    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/alpha",
    )
    monkeypatch.setenv("GPTBRIDGE_GOVERNED_REQUEST_ID", "repair-alpha-2")
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        service.extract_managed_backup(
            "alpha",
            ["alpha/runtime/state/data.txt"],
        )


def test_verified_main_backup_retires_legacy_file_but_keeps_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_project(tmp_path)
    legacy_root = tmp_path / "system-rescue" / "data" / "backups"
    legacy_root.mkdir(parents=True)
    legacy_file = legacy_root / "old-global.zip"
    legacy_file.write_bytes(b"legacy")
    unrelated = legacy_root / "do-not-delete.txt"
    unrelated.write_text("user data", encoding="utf-8")
    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/global-cleaner",
    )

    result = ProjectCleanupService(tmp_path).create_managed_backup("main-system")

    assert result["ok"] is True
    assert result["removed_legacy_excess"] == ["old-global.zip"]
    assert not legacy_file.exists()
    assert unrelated.read_text(encoding="utf-8") == "user data"
    assert legacy_root.is_dir()


def test_published_backup_failure_removes_incomplete_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_project(tmp_path)
    service = ProjectCleanupService(tmp_path)
    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/global-cleaner",
    )
    monkeypatch.setattr(
        service,
        "_verify_published_backup",
        lambda _owner_id, _path: (False, 0, "forced-published-failure"),
    )

    result = service.create_managed_backup("alpha")

    owner_root = (
        tmp_path
        / "global-cleaner"
        / "data"
        / "business"
        / "backups"
        / "alpha"
    )
    assert result["ok"] is False
    assert list(owner_root.glob("*.zip")) == []
    assert list(owner_root.glob("*.manifest.json")) == []


def test_extract_rejects_valid_zip_when_published_digest_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_project(tmp_path)
    service = ProjectCleanupService(tmp_path)
    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/global-cleaner",
    )
    created = service.create_managed_backup("alpha")
    assert created["ok"] is True
    archive = Path(created["backup"])
    with zipfile.ZipFile(archive, "a") as target:
        target.writestr("tampered-but-valid.txt", "changed")

    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/system-rescue",
    )
    monkeypatch.setenv("GPTBRIDGE_GOVERNED_REQUEST_ID", "repair-tampered")
    extracted = service.extract_managed_backup(
        "alpha",
        ["alpha/runtime/state/data.txt"],
    )
    listed = service.list_managed_backups()

    assert extracted["ok"] is False
    assert extracted["error_code"] == "BACKUP_INTEGRITY_FAILED"
    assert listed["ok"] is False
    assert listed["owners"]["alpha"][0]["integrity_verified"] is False


def test_backup_creation_respects_global_cleaner_mutation_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_project(tmp_path)
    service = ProjectCleanupService(tmp_path)
    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/global-cleaner",
    )

    with service._mutation_guard("test-holder") as lock:
        assert lock["acquired"] is True
        result = service.create_managed_backup("alpha")

    assert result["ok"] is False
    assert result["busy"] is True
    assert result["error_code"] == "CLEANER_BUSY"


class _FakeToolbox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def start_tool(self, payload):
        self.calls.append(("start", payload))
        return {"ok": True, "message": "started"}

    async def request_tool_execution(self, payload):
        self.calls.append(("request", payload))
        return {"ok": True, "queued": True}

    async def force_close_tool(self, payload):
        self.calls.append(("stop", payload))
        return {"ok": True}


class _FakeGovernance:
    def tool_execution_response(self, tool_id, request_id):
        return {
            "status": "completed",
            "response": {
                "ok": True,
                "tool_id": tool_id,
                "request_id": request_id,
            },
        }


class _FailingToolbox:
    async def start_tool(self, _payload):
        return {"ok": False, "error_code": "START_FAILED"}


class _CrashingToolbox:
    async def start_tool(self, _payload):
        raise RuntimeError("simulated launcher failure")


def test_daily_trigger_is_main_owned_and_uses_governed_shared_channel(
    tmp_path: Path,
) -> None:
    toolbox = _FakeToolbox()
    app = SimpleNamespace(
        project_root=tmp_path,
        toolbox_service=toolbox,
        system_sovereign_service=SimpleNamespace(
            permission_sovereign=_FakeGovernance()
        ),
    )
    service = DailyGlobalCleanerService(app)
    result = asyncio.run(service.run_if_due(force=True))

    assert result["ok"] is True
    assert [name for name, _payload in toolbox.calls] == ["start", "request", "stop"]
    request = toolbox.calls[1][1]
    assert request["tool_id"] == "global-cleaner"
    assert request["args"] == ["--governed-daily-maintenance", "--json"]
    assert service.status()["owner"] == "main-system"
    assert service.status()["channel"] == "governance-authenticated-shared-layer"


def test_daily_trigger_retries_failure_after_fifteen_minutes(
    tmp_path: Path,
) -> None:
    app = SimpleNamespace(
        project_root=tmp_path,
        toolbox_service=_FailingToolbox(),
        system_sovereign_service=SimpleNamespace(
            permission_sovereign=_FakeGovernance()
        ),
    )
    service = DailyGlobalCleanerService(app)
    result = asyncio.run(service.run_if_due(force=True))
    state = service._load_state()
    started = float(state["last_started_epoch"])

    assert result["ok"] is False
    assert service.is_due(started + service.FAILURE_RETRY_SECONDS - 1) is False
    assert service.is_due(started + service.FAILURE_RETRY_SECONDS) is True
    assert service.status()["next_due_epoch"] == (
        started + service.FAILURE_RETRY_SECONDS
    )


def test_daily_trigger_treats_corrupt_epoch_as_due(tmp_path: Path) -> None:
    app = SimpleNamespace(
        project_root=tmp_path,
        toolbox_service=_FailingToolbox(),
        governance=_FakeGovernance(),
    )
    service = DailyGlobalCleanerService(app)
    service.state_path.parent.mkdir(parents=True)
    service.state_path.write_text(
        json.dumps(
            {
                "last_started_epoch": "not-a-number",
                "last_ok": False,
            }
        ),
        encoding="utf-8",
    )

    assert service.is_due(now=service.FAILURE_RETRY_SECONDS) is True
    assert service.status()["next_due_epoch"] == service.FAILURE_RETRY_SECONDS


def test_daily_trigger_converts_start_exception_to_retryable_failure(
    tmp_path: Path,
) -> None:
    app = SimpleNamespace(
        project_root=tmp_path,
        toolbox_service=_CrashingToolbox(),
        system_sovereign_service=SimpleNamespace(
            permission_sovereign=_FakeGovernance()
        ),
    )
    service = DailyGlobalCleanerService(app)

    result = asyncio.run(service.run_if_due(force=True))
    state = service._load_state()

    assert result["ok"] is False
    assert result["detail"]["error_code"] == "GLOBAL_CLEANER_START_EXCEPTION"
    assert state["last_status"] == "start_failed"
    assert state["last_ok"] is False


def test_direct_mutation_cli_is_denied_without_governed_requester(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment.pop("GPTBRIDGE_GOVERNED_REQUESTER_ACTOR", None)
    environment["GPTBRIDGE_GLOBAL_CLEANER_TARGET_ROOT"] = str(tmp_path)
    completed = subprocess.run(
        [
            sys.executable,
            str(CLEANER_SRC / "main.py"),
            "--update-preferences",
            "--quarantine-ttl-hours",
            "24",
            "--json",
        ],
        cwd=str(ROOT / "global-cleaner"),
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    payload = json.loads(completed.stdout.strip())
    assert completed.returncode == 1
    assert payload == {
        "ok": False,
        "error_code": "PERMISSION_DENIED",
        "message": "PERMISSION_DENIED",
    }



########################################################################
# source: global-cleaner/tests/test_managed_temp_cleanup.py
########################################################################
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "global-cleaner" / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))


from backend.services.project_cleaner.application.service import ProjectCleanupService  # noqa: E402


def test_managed_temp_cleanup_deletes_files_and_preserves_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = tmp_path / "global-cleaner"
    nested = cleaner / "runtime" / "temp" / "tools" / "alpha" / "run" / "nested"
    nested.mkdir(parents=True)
    (cleaner / "manifest.json").write_text(
        json.dumps({"id": "global-cleaner", "version": "1.0.0"}),
        encoding="utf-8",
    )
    first = nested / "one.tmp"
    temp_run = cleaner / "runtime" / "temp" / "tools" / "alpha" / "run"
    second = temp_run / "two.sqlite3"
    first.write_text("one", encoding="utf-8")
    second.write_bytes(b"two")
    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/global-cleaner",
    )

    result = ProjectCleanupService(tmp_path).clear_managed_temp_files(
        "global-cleaner/runtime/temp/tools/alpha/run"
    )

    assert result["ok"] is True
    assert result["removed_files"] == 2
    assert result["directories_preserved"] is True
    assert nested.is_dir()
    assert list(temp_run.rglob("*")) == [nested]


def test_managed_temp_cleanup_rejects_paths_outside_tool_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = tmp_path / "global-cleaner"
    cleaner.mkdir()
    (cleaner / "manifest.json").write_text(
        json.dumps({"id": "global-cleaner"}), encoding="utf-8"
    )
    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/global-cleaner",
    )

    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        ProjectCleanupService(tmp_path).clear_managed_temp_files("alpha/runtime/temp")


def test_global_cleaner_clears_central_test_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleaner = tmp_path / "global-cleaner"
    artifact = (
        cleaner
        / "runtime"
        / "temp"
        / "development"
        / "test-artifacts"
        / "run-1"
        / "pytest-cache"
    )
    artifact.mkdir(parents=True)
    (cleaner / "manifest.json").write_text(
        json.dumps({"id": "global-cleaner", "version": "1.0.0"}),
        encoding="utf-8",
    )
    (artifact / "nodeids").write_text("[]", encoding="utf-8")
    monkeypatch.setenv(
        "GPTBRIDGE_GOVERNED_REQUESTER_ACTOR",
        "governance/tool/global-cleaner",
    )

    result = ProjectCleanupService(tmp_path).clear_managed_temp_files(
        "global-cleaner/runtime/temp/development/test-artifacts"
    )

    assert result["ok"] is True
    assert result["removed_files"] == 1
    assert artifact.is_dir()
    assert not list(artifact.iterdir())



########################################################################
# source: global-cleaner/tests/test_ai_assistant_layering.py
########################################################################
import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "ai-assistant" / "src" / "backend" / "services" / "ai_nexus"


def test_ai_assistant_has_owned_layers() -> None:
    for layer in ("application", "domain", "infrastructure", "integration"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]


def test_ai_assistant_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (ROOT / "ai-assistant" / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_ai_assistant_has_no_direct_network_client() -> None:
    forbidden = {
        "aiohttp": re.compile(r"^\s*(?:from|import)\s+aiohttp(?:\.|\s|$)", re.MULTILINE),
        "httpx": re.compile(r"^\s*(?:from|import)\s+httpx(?:\.|\s|$)", re.MULTILINE),
        "requests": re.compile(r"^\s*(?:from|import)\s+requests(?:\.|\s|$)", re.MULTILINE),
        "smtp": re.compile(r"^\s*(?:from|import)\s+smtplib(?:\.|\s|$)", re.MULTILINE),
        "urllib-request": re.compile(r"urllib\.request|\burlopen\s*\(", re.MULTILINE),
    }
    violations: list[str] = []
    for source in PACKAGE.rglob("*.py"):
        content = source.read_text(encoding="utf-8")
        matches = sorted(name for name, pattern in forbidden.items() if pattern.search(content))
        if matches:
            violations.append(f"{source.relative_to(ROOT)}: {', '.join(matches)}")
    assert violations == []


def test_ai_nexus_is_not_imported_by_other_tools() -> None:
    pattern = re.compile(r"^\s*(?:from|import)\s+ai_nexus(?:\.|\s|$)", re.MULTILINE)
    violations: list[str] = []
    for source in ROOT.glob("*/src/**/*.py"):
        if source.is_relative_to(ROOT / "ai-assistant"):
            continue
        if pattern.search(source.read_text(encoding="utf-8")):
            violations.append(source.relative_to(ROOT).as_posix())
    assert violations == []



########################################################################
# source: global-cleaner/tests/test_ai_collaboration_layering.py
########################################################################
import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "ai-collaboration"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "ai_collaboration"

from ai_collaboration.infrastructure.repository import AiCollaborationRepository


def test_ai_collaboration_has_owned_layers() -> None:
    for layer in ("application", "domain", "infrastructure", "integration"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]


def test_ai_collaboration_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_ai_collaboration_database_is_tool_owned(tmp_path: Path) -> None:
    repository = AiCollaborationRepository(tmp_path)
    database = repository.db_path.resolve()
    assert database.is_relative_to(tmp_path.resolve())
    assert database.name == "ai_collaboration.sqlite3"
    assert database.is_file()



########################################################################
# source: global-cleaner/tests/test_ai_channel_governance.py
########################################################################
import asyncio
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SHARED_SRC = ROOT / "shared-layer" / "src"
AI_COLLABORATION_SERVICES = (
    ROOT / "ai-collaboration" / "src" / "backend" / "services"
)
for path in (ROOT, SHARED_SRC, AI_COLLABORATION_SERVICES):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


from governance_rule.permission_directory.registries.permissions.tool_routes import (  # noqa: E402
    XINGCHENG_AUTOMATIC_WORKFLOW_SEQUENCE,
    ai_channel_status,
    authorize_ai_route,
    authorize_ai_target,
    authorize_xingcheng_automatic_workflow,
)
from ai_collaboration.domain.task_protocol import build_ai_task_envelope  # noqa: E402
from ai_collaboration.integration.provider_session import (  # noqa: E402
    AiCollaborationProviderSession,
)


def test_only_star_can_request_external_collaboration() -> None:
    assert (
        authorize_ai_route(
            "governance/tool/xingcheng",
            "ai-collaboration",
            "ai_nexus_send_message",
        )
        == "xingcheng"
    )
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(
            "governance/tool/ai-assistant",
            "ai-collaboration",
            "ai_nexus_send_message",
        )


def test_investment_manager_can_only_request_star_commands() -> None:
    assert (
        authorize_ai_route(
            "governance/tool/ai-assistant",
            "xingcheng",
            "xingcheng_analyze_investments",
        )
        == "ai-assistant"
    )
    assert (
        authorize_ai_route(
            "governance/tool/ai-assistant",
            "xingcheng",
            "xingcheng_manage_investment_accounting",
        )
        == "ai-assistant"
    )
    assert (
        authorize_ai_route(
            "governance/tool/ai-assistant",
            "xingcheng",
            "xingcheng_discuss_investment_analysis",
        )
        == "ai-assistant"
    )
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(
            "governance/tool/ai-assistant",
            "xingcheng",
            "ai_nexus_send_message",
        )


def test_external_ai_cannot_route_a_response_to_investment_manager() -> None:
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(
            "governance/tool/ai-collaboration",
            "ai-assistant",
            "investment_ai_consult",
        )


def test_target_accepts_only_the_governed_route() -> None:
    authorize_ai_target(
        "governance/tool/xingcheng",
        "ai-collaboration",
        "ai_nexus_send_message",
    )
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_target(
            "governance/tool/ai-assistant",
            "ai-collaboration",
            "ai_nexus_send_message",
        )


def test_governance_can_manage_target_without_becoming_ai_participant() -> None:
    authorize_ai_target(
        "governance/main-system",
        "xingcheng",
        "xingcheng_status",
    )
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(
            "governance/main-system",
            "ai-collaboration",
            "ai_nexus_send_message",
        )


def test_status_declares_governance_and_star_authority() -> None:
    status = ai_channel_status()
    assert status["channel_id"] == "shared-layer/ai-channel"
    assert status["highest_authority"] == "governance-rule"
    assert status["channel_top_level_tool"] == "xingcheng"
    assert status["external_ai_response_recipient"] == "xingcheng"
    assert status["investment_manager_external_ai"] is False
    assert status["xingcheng_automatic_workflow"]["excluded_path_roots"] == [
        "governance_rule"
    ]


def test_governance_validates_the_xingcheng_automatic_workflow() -> None:
    payload = {
        "automatic_workflow": True,
        "autonomous_agent": True,
        "workflow_sequence": list(XINGCHENG_AUTOMATIC_WORKFLOW_SEQUENCE),
        "primary_language": "zh-TW",
    }
    authorize_xingcheng_automatic_workflow(
        "governance/tool/star-chat",
        "xingcheng",
        "xingcheng_infer",
        payload,
    )
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_xingcheng_automatic_workflow(
            "governance/tool/star-chat",
            "xingcheng",
            "xingcheng_infer",
            {**payload, "workflow_sequence": ["execute", "result"]},
        )


def test_star_chat_has_only_status_and_inference_routes() -> None:
    assert authorize_ai_route(
        "governance/tool/star-chat", "xingcheng", "xingcheng_infer"
    ) == "star-chat"
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(
            "governance/tool/star-chat",
            "xingcheng",
            "xingcheng_train_with_gpt",
        )


def test_task_envelope_rejects_investment_manager_and_sanitizes_memory() -> None:
    task = build_ai_task_envelope(
        provider="gemini",
        business_scope="investment",
        task_type="search",
        content="搜尋官方資料",
        requested_by="xingcheng",
        memory_context=[
            {
                "memory_id": "m1",
                "kind": "source",
                "title": "既有手動設定",
                "content": "未有可靠更新時保留手動配息頻率",
                "origin_model_id": "star-investment-native-model",
                "business_scope": "investment",
                "private_field": "must-not-cross-channel",
            }
        ],
    )

    assert task["schema_version"] == "1.0"
    assert task["response_recipient"] == "xingcheng"
    assert task["memory_policy"]["direct_database_access"] is False
    assert "private_field" not in task["memory_context"][0]
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        build_ai_task_envelope(
            provider="gemini",
            business_scope="investment",
            task_type="search",
            content="越權搜尋",
            requested_by="ai-assistant",
        )


def test_all_external_ai_use_same_provider_browser_without_fallback(
    tmp_path: Path,
) -> None:
    session = AiCollaborationProviderSession(tmp_path)

    class FakeBrowser:
        async def send_prompt(
            self, agent: dict[str, object], _prompt: str
        ) -> dict[str, object]:
            return {
                "status": "completed",
                "provider": agent["provider"],
                "content": "browser response",
                "transport": "embedded-browser-view",
                "uses_api_key": False,
                "fallback": {
                    "used": False,
                    "browser_only": True,
                    "cross_provider_substitution": False,
                },
                "memory_candidates": [],
            }

    session.browser = FakeBrowser()  # type: ignore[assignment]
    task = build_ai_task_envelope(
        provider="gemini",
        business_scope="investment",
        task_type="advanced_search",
        content="尋找官方配息資料",
        requested_by="xingcheng",
    )

    result = asyncio.run(
        session.send_task({"provider": "gemini", "agent_id": "gemini"}, task)
    )

    assert result["status"] == "completed"
    assert result["provider"] == "gemini"
    assert result["transport"] == "embedded-browser-view"
    assert result["fallback"]["used"] is False
    assert result["fallback"]["cross_provider_substitution"] is False
    assert result["memory_candidates"][0]["direct_database_write"] is False


def test_provider_status_declares_all_six_as_browser_automated(tmp_path: Path) -> None:
    session = AiCollaborationProviderSession(tmp_path)
    status = session.provider_status()

    assert {item["provider"] for item in status} == {
        "chatgpt",
        "claude",
        "gemini",
        "grok",
        "deepseek",
        "perplexity",
    }
    assert all(item["browser_only"] is True for item in status)
    assert all(item["automation"] is True for item in status)
    assert all(item["terminal_fallback"] is False for item in status)



########################################################################
# source: global-cleaner/tests/test_file_sorter_layering.py
########################################################################
import ast
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "file-sorter"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "file_sorter"

from file_sorter.infrastructure.cleanup import VideoFingerprintCache
from file_sorter.infrastructure.sorter_engine import resolve_state_root


def test_file_sorter_has_owned_layers_and_thin_entries() -> None:
    for layer in ("application", "domain", "infrastructure"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]
    assert len((TOOL_ROOT / "src" / "main.py").read_text(encoding="utf-8").splitlines()) <= 10
    channel_source = (TOOL_ROOT / "src" / "channel_runtime.py").read_text(encoding="utf-8")
    assert "GovernedToolRuntime" in channel_source
    assert "websockets.serve" not in channel_source


def test_file_sorter_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_file_sorter_database_defaults_to_tool_folder() -> None:
    cache = VideoFingerprintCache()
    assert cache.database_path.resolve().is_relative_to(TOOL_ROOT.resolve())
    assert cache.database_path.name == "video-fingerprints.sqlite3"


def test_file_sorter_rejects_external_state_root(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="FILE_SORTER_STATE_SCOPE_DENIED"):
        resolve_state_root(tmp_path)



########################################################################
# source: global-cleaner/tests/test_investment_mobile_governance.py
########################################################################
import importlib.util
import socket
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from governance_rule.permission_directory.registries.permissions.tool_routes import (
    authorize_ai_route,
    authorize_investment_mobile_route,
)


def test_mobile_connects_to_star_not_directly_to_investment_manager() -> None:
    authorize_investment_mobile_route(
        "governance/tool/investment-mobile",
        "xingcheng",
        "xingcheng_mobile_get_investment_snapshot",
    )
    with pytest.raises(PermissionError):
        authorize_investment_mobile_route(
            "governance/tool/investment-mobile",
            "ai-assistant",
            "investment_mobile_get_snapshot",
        )


def test_only_star_can_proxy_mobile_commands_to_investment_manager() -> None:
    assert (
        authorize_ai_route(
            "governance/tool/xingcheng",
            "ai-assistant",
            "investment_mobile_get_snapshot",
        )
        == "xingcheng"
    )
    with pytest.raises(PermissionError):
        authorize_ai_route(
            "governance/tool/investment-mobile",
            "ai-assistant",
            "investment_mobile_get_snapshot",
        )


def test_investment_manager_process_network_policy_blocks_external_hosts() -> None:
    module_path = ROOT / "ai-assistant" / "src" / "investment_network_policy.py"
    spec = importlib.util.spec_from_file_location("investment_network_policy_test", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.install_investment_manager_network_policy()

    with pytest.raises(PermissionError):
        socket.getaddrinfo("example.com", 443)
    assert socket.getaddrinfo("127.0.0.1", 80)



########################################################################
# source: global-cleaner/tests/test_investment_mobile_layering.py
########################################################################
import ast
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "investment-mobile"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "investment_mobile"
repository_path = PACKAGE / "infrastructure" / "repository.py"
repository_spec = importlib.util.spec_from_file_location(
    "investment_mobile_compatibility_repository",
    repository_path,
)
assert repository_spec and repository_spec.loader
repository_module = importlib.util.module_from_spec(repository_spec)
repository_spec.loader.exec_module(repository_module)
InvestmentMobileRepository = repository_module.InvestmentMobileRepository


def test_investment_mobile_has_owned_layers() -> None:
    for layer in (
        "application",
        "domain",
        "infrastructure",
        "integration",
        "presentation",
    ):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]


def test_investment_mobile_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_investment_mobile_compatibility_layer_cannot_create_a_database(
    tmp_path: Path,
) -> None:
    repository = InvestmentMobileRepository(tmp_path)
    assert repository.persistence_owner == "ai-assistant"
    assert repository.database_path is None
    assert repository.setting("enabled", "missing") == "false"
    repository.set_setting("enabled", "true")
    assert repository.setting("enabled", "missing") == "true"
    assert not list(tmp_path.rglob("*.sqlite3"))



########################################################################
# source: global-cleaner/tests/test_main_system_boundaries.py
########################################################################
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "main-system"


def test_main_system_has_no_tool_business_modules() -> None:
    assert not (MAIN / "src-core" / "managers" / "provider_monitor.py").exists()
    assert not (MAIN / "scripts" / "smoke" / "ai_assistant_visual_smoke.py").exists()
    assert (ROOT / "ai-assistant" / "scripts" / "visual_smoke.py").is_file()
    assert not (MAIN / "scripts" / "package_platform_tools.py").exists()
    assert (
        ROOT
        / "system-rescue"
        / "src"
        / "backend"
        / "services"
        / "system_rescue"
        / "integration"
        / "platform_packager.py"
    ).is_file()


def test_main_toolbox_lifecycle_is_modularized() -> None:
    task_root = MAIN / "src-core" / "tasks"
    assert (task_root / "tool_path_resolver.py").is_file()
    assert (task_root / "tool_process_registry.py").is_file()
    toolbox = (task_root / "toolbox_service.py").read_text(encoding="utf-8")
    assert "ToolPathResolver" in toolbox
    assert "GPTBRIDGE_SOURCE_RUNTIME_ENTRY" not in toolbox
    assert "GPTBRIDGE_SOURCE_UI_TOOL_ID_QUERY" not in toolbox


def test_main_core_has_no_provider_or_investment_implementation() -> None:
    forbidden = {
        "ai_nexus",
        "chatgpt",
        "claude",
        "deepseek",
        "dividend",
        "gemini",
        "grok",
        "holdings",
        "investment_mobile",
        "perplexity",
        "portfolio",
    }
    violations: list[str] = []
    for source in (MAIN / "src-core").rglob("*.py"):
        content = source.read_text(encoding="utf-8").casefold()
        matches = sorted(term for term in forbidden if term in content)
        if matches:
            violations.append(f"{source.relative_to(ROOT)}: {', '.join(matches)}")
    assert violations == []


def test_capacity_inventory_exposes_shared_layer_and_true_project_total() -> None:
    size_inventory = (
        MAIN / "src-ui" / "main" / "platform-tool-sizes.ts"
    ).read_text(encoding="utf-8")
    main_ipc = (MAIN / "src-ui" / "main" / "index.ts").read_text(
        encoding="utf-8"
    )
    main_ui = (MAIN / "src-ui" / "renderer" / "ui" / "App.tsx").read_text(
        encoding="utf-8"
    )
    cleaner_ui = (
        ROOT / "global-cleaner" / "src" / "ui" / "ProjectCleanerWindowApp.tsx"
    ).read_text(encoding="utf-8")

    assert "getSharedLayerSize" in size_inventory
    assert "shared_layer: sharedLayer" in main_ipc
    assert 'data-testid="shared-layer-folder-size"' in main_ui
    assert "formatProjectSize(analysis.total_bytes)" in cleaner_ui


def test_main_typescript_has_no_tool_business_knowledge() -> None:
    forbidden = {
        "chatgpt",
        "claude",
        "deepseek",
        "dividend",
        "gemini",
        "grok",
        "holdings",
        "perplexity",
        "portfolio",
    }
    violations: list[str] = []
    for source in (MAIN / "src-ui").rglob("*"):
        if source.suffix not in {".ts", ".tsx", ".js", ".cjs"}:
            continue
        content = source.read_text(encoding="utf-8").casefold()
        matches = sorted(term for term in forbidden if term in content)
        if matches:
            violations.append(f"{source.relative_to(ROOT)}: {', '.join(matches)}")
    assert violations == []



########################################################################
# source: global-cleaner/tests/test_main_system_governance_health.py
########################################################################
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAIN_CORE = ROOT / "main-system" / "src-core"
SHARED_LAYER_SRC = ROOT / "shared-layer" / "src"
for source_root in (MAIN_CORE, SHARED_LAYER_SRC):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))


from core_system.governance_runtime import MainSystemGovernance  # noqa: E402
from tasks.toolbox_service import ToolboxService  # noqa: E402


class _IntegrityAuthentication:
    def __init__(self, *, denied: bool = False) -> None:
        self.denied = denied
        self.calls = 0

    def verify_runtime_integrity(self) -> None:
        self.calls += 1
        if self.denied:
            raise PermissionError("PERMISSION_DENIED")


def _runtime(authentication: _IntegrityAuthentication) -> MainSystemGovernance:
    runtime = MainSystemGovernance.__new__(MainSystemGovernance)
    runtime._authentication = authentication
    runtime._integrity_ready = True
    runtime._integrity_checked_at = 0.0
    return runtime


def test_runtime_integrity_health_reports_current_authority() -> None:
    authentication = _IntegrityAuthentication()
    runtime = _runtime(authentication)

    assert runtime.runtime_integrity_ready(max_age_seconds=0) is True
    assert authentication.calls == 1


def test_runtime_integrity_health_rejects_stale_launch_credential() -> None:
    authentication = _IntegrityAuthentication(denied=True)
    runtime = _runtime(authentication)

    assert runtime.runtime_integrity_ready(max_age_seconds=0) is False
    assert authentication.calls == 1


def test_daily_cleaner_uses_governed_source_without_opening_ui() -> None:
    manifest = {
        "launch": {
            "primary": "executable",
            "background": "governed-source-channel",
        },
        "request_channel": {
            "model": "governance-authenticated-shared-layer",
            "runtime_entry": "src/channel_runtime.py",
            "direct_instruction": "PERMISSION_DENIED",
        },
    }

    assert ToolboxService._source_launch_requested(
        manifest,
        background=True,
        requested_mode="source",
        executable_exists=True,
    ) is True
    assert ToolboxService._source_launch_requested(
        manifest,
        background=False,
        requested_mode="",
        executable_exists=True,
    ) is False
    assert ToolboxService._source_launch_requested(
        manifest,
        background=True,
        requested_mode="executable",
        executable_exists=True,
    ) is False


def test_global_cleaner_dual_runtime_selects_an_available_mode() -> None:
    manifest = {
        "launch": {
            "mode": "dual-runtime",
            "selection": "automatic",
            "runtimes": ["governed-source-ui", "executable"],
            "background": "governed-source-channel",
        },
        "request_channel": {
            "model": "governance-authenticated-shared-layer",
            "runtime_entry": "src/channel_runtime.py",
            "direct_instruction": "PERMISSION_DENIED",
        },
    }

    assert ToolboxService._source_launch_requested(
        manifest,
        background=False,
        requested_mode="",
        executable_exists=False,
    ) is True
    assert ToolboxService._source_fallback_allowed(manifest, "executable") is True
    assert ToolboxService._executable_fallback_allowed(
        manifest,
        "source",
        executable_exists=True,
    ) is True
    assert ToolboxService._executable_fallback_allowed(
        manifest,
        "source",
        executable_exists=False,
    ) is False
    assert ToolboxService._source_launch_requested(
        manifest,
        background=False,
        requested_mode="executable",
        executable_exists=False,
    ) is False



########################################################################
# source: global-cleaner/tests/test_shared_layer_dual_channels.py
########################################################################
import json
from pathlib import Path

from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
    capability_boundary_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)


def test_system_and_ai_channels_have_separate_capabilities_and_databases() -> None:
    capabilities, _repairs = capability_boundary_snapshot()
    by_name = {item.capability: item for item in capabilities}

    system_submit = by_name["system-channel-request-submit"]
    ai_submit = by_name["ai-channel-request-submit"]
    assert system_submit.grants[0].path_roots == (
        "postgresql:gptbridge_transport:system",
    )
    assert ai_submit.grants[0].path_roots == (
        "postgresql:gptbridge_transport:ai",
    )


def test_ai_channel_permissions_are_participant_scoped() -> None:
    bindings = {
        item.actor: set(item.capabilities)
        for item in identity_permission_snapshot()
    }
    assert "ai-channel-request-process" in bindings["governance/tool/xingcheng"]
    assert "ai-channel-request-process" in bindings["governance/tool/ai-assistant"]
    assert "ai-channel-request-process" in bindings[
        "governance/tool/ai-collaboration"
    ]
    assert "ai-channel-request-submit" in bindings[
        "governance/tool/investment-mobile"
    ]
    assert "ai-channel-request-process" not in bindings[
        "governance/tool/investment-mobile"
    ]
    assert "ai-channel-request-submit" not in bindings[
        "governance/tool/file-sorter"
    ]


def test_star_programming_excludes_governance_and_has_project_database_access() -> None:
    capabilities, _repairs = capability_boundary_snapshot()
    by_name = {item.capability: item for item in capabilities}
    database = by_name["star-investment-manager-database-read"]

    assert database.owner == "tool:xingcheng"
    assert [(grant.action, grant.target) for grant in database.grants] == [
        ("read", "tool-business-storage:ai-assistant"),
    ]
    assert database.grants[0].data_scope == "ai-assistant-investment-database"

    programming_capability = json.loads(
        (Path(__file__).resolve().parents[2] / "local-model" / "manifest.json").read_text(
            "utf-8"
        )
    )["capabilities"]["star-project-programming"]
    assert programming_capability["excluded_path_roots"] == ["governance_rule"]



########################################################################
# source: global-cleaner/tests/test_shared_layer_ownership.py
########################################################################
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SHARED_PACKAGE = ROOT / "shared-layer" / "src" / "shared_layer"


def test_shared_layer_contains_transport_only() -> None:
    # Shared-layer is the platform transport/identity layer for the star tool
    # (xingcheng/xingcheng). It must not leak code or references from any OTHER
    # business tool or product.
    forbidden_terms = {
        "chatgpt",
        "claude",
        "gemini",
        "grok",
        "deepseek",
        "perplexity",
        "google-search",
        "ai-assistant",
        "ai-collaboration",
        "investment-mobile",
        "holdings",
        "portfolio",
    }
    violations: list[str] = []
    for source in SHARED_PACKAGE.rglob("*.py"):
        text = source.read_text(encoding="utf-8").casefold()
        matches = sorted(term for term in forbidden_terms if term in text)
        if matches:
            violations.append(f"{source.relative_to(ROOT)}: {', '.join(matches)}")
    assert violations == []


def test_business_modules_have_explicit_owners() -> None:
    assert (
        ROOT
        / "governance_rule"
        / "permission_directory"
        / "registries"
        / "permissions"
        / "tool_routes.py"
    ).is_file()
    assert (
        ROOT
        / "ai-collaboration"
        / "src"
        / "backend"
        / "services"
        / "ai_collaboration"
        / "integration"
        / "provider_gateway.py"
    ).is_file()
    assert (
        ROOT
        / "investment-mobile"
        / "src"
        / "backend"
        / "services"
        / "investment_mobile"
        / "integration"
        / "channel_client.py"
    ).is_file()


def test_deprecated_shared_business_packages_have_no_source() -> None:
    for directory_name in ("ai_channel", "mobile_channel"):
        directory = SHARED_PACKAGE / directory_name
        assert list(directory.glob("*.py")) == []



########################################################################
# source: global-cleaner/tests/test_shared_layer_retention.py
########################################################################
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_SRC = ROOT / "shared-layer" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))


from shared_layer.store import SharedLayerStore  # noqa: E402


def test_store_uses_postgres_transport_terminating_consumed_rows() -> None:
    source = Path(
        __file__
    ).resolve().parents[2] / "shared-layer" / "src" / "shared_layer" / "store.py"
    text = source.read_text("utf-8")

    assert hasattr(SharedLayerStore, "consume_response")
    assert "gptbridge_transport.tool_request" in text
    assert "pg_notify" in text
    assert "FOR UPDATE SKIP LOCKED" in text

    assert "def _maintain_requests" not in text
    assert "_last_request_maintenance" not in text



########################################################################
# source: global-cleaner/tests/test_vaultly_layering.py
########################################################################
import ast
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "vaultly"
SERVICES = TOOL_ROOT / "src" / "backend" / "services"
PACKAGE = SERVICES / "vaultly"

from vaultly.infrastructure.repository import VaultlyRepository


def test_vaultly_has_owned_layers() -> None:
    for layer in ("application", "domain", "infrastructure", "integration"):
        assert (PACKAGE / layer / "__init__.py").is_file()
    assert [path.name for path in PACKAGE.glob("*.py")] == ["__init__.py"]


def test_vaultly_sources_parse_after_split() -> None:
    failures: list[str] = []
    for source in (TOOL_ROOT / "src").rglob("*.py"):
        try:
            ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (OSError, SyntaxError, UnicodeError) as error:
            failures.append(f"{source.relative_to(ROOT)}: {error}")
    assert failures == []


def test_vaultly_database_is_tool_owned(tmp_path: Path) -> None:
    repository = VaultlyRepository(tmp_path)
    database = repository.db_path.resolve()
    assert database.is_relative_to(tmp_path.resolve())
    assert database.name == "vaultly.sqlite3"
    assert database.is_file()

