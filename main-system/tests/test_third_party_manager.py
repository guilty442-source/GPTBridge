"""Tests for the ThirdPartyManager centralized version management service."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src-core"))

from core_system.third_party_manager import (  # noqa: E402
    AUTO_UPDATABLE_TOOLS,
    THIRD_PARTY_MANAGER_VERSION,
    ThirdPartyManager,
    ToolVersionInfo,
    _normalize_version,
)


@pytest.fixture
def inventory_file(tmp_path: Path) -> Path:
    """Create a minimal tool_inventory.json for testing."""
    data = {
        "schema": "governance-tool-inventory-v1",
        "tools": [
            {
                "id": "git",
                "name": "Git",
                "version": "2.45.0",
                "path": "git",
                "license": "GPL-2.0",
                "status": "active",
            },
            {
                "id": "uv",
                "name": "uv",
                "version": "0.5.0",
                "path": "uv",
                "license": "MIT",
                "status": "active",
            },
            {
                "id": "ollama",
                "name": "Ollama",
                "version": "0.4.0",
                "path": "ollama",
                "license": "MIT",
                "status": "active",
            },
        ],
    }
    path = tmp_path / "tool_inventory.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


class TestNormalizeVersion:
    def test_strips_v_prefix(self) -> None:
        assert _normalize_version("v22.3.0") == "22.3.0"

    def test_stips_build_suffix(self) -> None:
        assert _normalize_version("2.45.0.windows.1").startswith("2.45.0")

    def test_casefold(self) -> None:
        assert _normalize_version("V1.0.0") == "1.0.0"

    def test_empty(self) -> None:
        assert _normalize_version("") == ""


class TestThirdPartyManagerInit:
    def test_version_constant(self) -> None:
        assert THIRD_PARTY_MANAGER_VERSION == "1.0.0"

    def test_load_inventory(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        inv = manager.load_inventory()
        assert inv is not None
        assert len(inv["tools"]) == 3

    def test_load_inventory_missing_file(self, tmp_path: Path) -> None:
        manager = ThirdPartyManager(tmp_path / "nonexistent.json")
        assert manager.load_inventory() is None

    def test_load_inventory_invalid_json(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{invalid", encoding="utf-8")
        manager = ThirdPartyManager(bad)
        assert manager.load_inventory() is None

    def test_get_recorded_version(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        assert manager.get_recorded_version("git") == "2.45.0"
        assert manager.get_recorded_version("uv") == "0.5.0"
        assert manager.get_recorded_version("nonexistent") == ""

    def test_is_auto_updatable(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        assert manager.is_auto_updatable("uv") is True
        assert manager.is_auto_updatable("npm") is True
        assert manager.is_auto_updatable("ollama") is True
        assert manager.is_auto_updatable("git") is False
        assert manager.is_auto_updatable("python") is False


class TestVersionProbe:
    def test_probe_version_success(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        with patch("shutil.which", return_value="/usr/bin/git"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.stdout = "git version 2.45.0\n"
                mock_run.return_value.stderr = ""
                info = manager.probe_version("git")
        assert info.detected is True
        assert info.detected_version == "2.45.0"
        assert info.error == ""

    def test_probe_version_not_found(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        with patch("shutil.which", return_value=None):
            info = manager.probe_version("git")
        assert info.detected is False
        assert "not found" in info.error

    def test_probe_version_no_command(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        info = manager.probe_version("unknown-tool")
        assert info.detected is False
        assert "no probe command" in info.error

    def test_probe_version_pattern_not_found(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        with patch("shutil.which", return_value="/usr/bin/git"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.stdout = "unexpected output\n"
                mock_run.return_value.stderr = ""
                info = manager.probe_version("git")
        assert info.detected is False
        assert "pattern not found" in info.error

    def test_probe_all_versions(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        with patch("shutil.which", return_value="/usr/bin/git"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.stdout = "git version 2.45.0\n"
                mock_run.return_value.stderr = ""
                results = manager.probe_all_versions()
        assert "git" in results
        assert "uv" in results
        assert "ollama" in results
        assert results["git"].detected is True


class TestToolVersionInfo:
    def test_status_ok(self) -> None:
        info = ToolVersionInfo(
            tool_id="git",
            recorded_version="2.45.0",
            detected_version="2.45.0",
            detected=True,
        )
        assert info.status == "ok"
        assert info.version_matches is True

    def test_status_version_drift(self) -> None:
        info = ToolVersionInfo(
            tool_id="git",
            recorded_version="2.45.0",
            detected_version="2.46.0",
            detected=True,
        )
        assert info.status == "version-drift"
        assert info.version_matches is False

    def test_status_missing(self) -> None:
        info = ToolVersionInfo(tool_id="git", recorded_version="2.45.0")
        assert info.status == "missing"

    def test_status_error(self) -> None:
        info = ToolVersionInfo(tool_id="git", error="probe failed")
        assert info.status == "error"

    def test_as_dict(self) -> None:
        info = ToolVersionInfo(
            tool_id="git",
            recorded_version="2.45.0",
            detected_version="2.45.0",
            detected=True,
        )
        d = info.as_dict()
        assert d["tool_id"] == "git"
        assert d["status"] == "ok"
        assert d["version_matches"] is True


class TestUpdateExecution:
    @pytest.mark.asyncio
    async def test_execute_update_not_auto_updatable(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        result = await manager.execute_update("git", approval_token="test")
        assert result.ok is False
        assert "not auto-updatable" in result.error

    @pytest.mark.asyncio
    async def test_execute_update_no_approval(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        result = await manager.execute_update("uv", approval_token=None)
        assert result.ok is False
        assert "approval token required" in result.error

    @pytest.mark.asyncio
    async def test_execute_update_not_in_path(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        with patch("shutil.which", return_value=None):
            result = await manager.execute_update("uv", approval_token="test")
        assert result.ok is False
        assert "not found" in result.error


class TestStatus:
    def test_get_status_empty(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        status = manager.get_status()
        assert status["version"] == THIRD_PARTY_MANAGER_VERSION
        assert status["inventory_path"] == str(inventory_file)
        assert "uv" in status["auto_updatable_tools"]
        assert status["versions"] == {}

    def test_get_status_after_probe(self, inventory_file: Path) -> None:
        manager = ThirdPartyManager(inventory_file)
        with patch("shutil.which", return_value="/usr/bin/git"):
            with patch("subprocess.run") as mock_run:
                mock_run.return_value.stdout = "git version 2.45.0\n"
                mock_run.return_value.stderr = ""
                manager.probe_version("git")
        status = manager.get_status()
        assert "git" in status["versions"]
        assert status["versions"]["git"]["detected_version"] == "2.45.0"


class TestAutoUpdatableTools:
    def test_auto_updatable_set_contents(self) -> None:
        assert "uv" in AUTO_UPDATABLE_TOOLS
        assert "npm" in AUTO_UPDATABLE_TOOLS
        assert "ollama" in AUTO_UPDATABLE_TOOLS
        assert "electron" in AUTO_UPDATABLE_TOOLS
        assert "git" not in AUTO_UPDATABLE_TOOLS
        assert "python" not in AUTO_UPDATABLE_TOOLS
        assert "postgresql" not in AUTO_UPDATABLE_TOOLS
