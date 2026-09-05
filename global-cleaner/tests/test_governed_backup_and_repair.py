from __future__ import annotations

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
