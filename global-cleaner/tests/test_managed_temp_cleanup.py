from __future__ import annotations

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
