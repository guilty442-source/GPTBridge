"""Split from consolidated test_main_system.py (main-system/tests/test_project_contract_matrix.py)."""
from __future__ import annotations

import _main_system_test_support as _support  # noqa: F401
from _main_system_test_support import (ROOT, _read_text_cached, _parse_python_cached,
    EXPECTED_TOOL_IDS, GOVERNANCE_TOOL_ID, SIBLING_IMPORT_ROOTS,
    MANIFEST_PATHS, TOOL_CASES, NON_GOVERNANCE_CASES)
from _test_project_contract_matrix_helpers import _load_json

import ast
import json
import re
import subprocess
from functools import cache
from pathlib import Path
from typing import Any
import pytest
from governance_rule.code_rule_directory import (  # noqa: E402
    code_rule_directory_snapshot,
)

def test_launcher_entrypoints_delegate_to_no_window_hosts() -> None:
    for filename in ("run-interface.bat", "啟動主程式.bat", "自動化工具.bat"):
        source = (ROOT / "main-system" / filename).read_text("utf-8")
        assert "wscript.exe //B //Nologo" in source
        assert "powershell.exe" not in source.casefold()

    vbs_source = (ROOT / "main-system" / "start-hidden.vbs").read_text("utf-8")
    assert 'shell.Run "powershell.exe' in vbs_source
    assert '", 0, False' in vbs_source

    launcher_source = (
        ROOT / "main-system" / "launcher" / "src" / "GPTBridgeLauncher.cpp"
    ).read_text("utf-8")
    assert "CREATE_NO_WINDOW" in launcher_source
    assert "SW_HIDE" in launcher_source


def test_manifest_test_targets_resolve_to_real_test_files() -> None:
    checked = 0
    for manifest_path in MANIFEST_PATHS:
        manifest = _load_json(manifest_path)
        targets = manifest.get("test_targets", [])
        assert targets, manifest["id"]
        for raw_target in targets:
            candidates = [
                manifest_path.parent / str(raw_target),
                ROOT / "main-system" / str(raw_target),
            ]
            assert any(candidate.is_file() for candidate in candidates), (
                manifest["id"],
                raw_target,
            )
        checked += 1
    assert checked == len(MANIFEST_PATHS)


def test_runtime_contract_matches_every_governed_request_channel() -> None:
    contract = _load_json(ROOT / "main-system" / "config" / "tool-runtime-contract.json")
    shared_model = contract["shared_layer"]["request_model"]
    direct_instruction = contract["shared_layer"]["direct_cross_tool_instruction"]
    for _tool_id, manifest_path in NON_GOVERNANCE_CASES:
        channel = _load_json(manifest_path)["request_channel"]
        assert channel["model"] == shared_model
        assert channel["direct_instruction"] == direct_instruction


def test_project_root_contains_only_governed_modules_and_control_files() -> None:
    governed_modules = {
        path.parent.name
        for path in ROOT.glob("*/manifest.json")
    }
    allowed_directories = governed_modules | {
        ".git",
        ".idea",
        ".vs",
        ".vscode",
        ".devin",
        ".venv",
        ".smallcode",
        "main-system",
        "shared-layer",
        "docs",
        "scripts",
        "launcher",
        "native",
        "Standalone tools",
    }
    allowed_files = {".gitignore", "pytest.ini", ".env", ".markdownlint.json", "AGENTS.md"}
    allowed_directories = allowed_directories | {
        ".kilo",
        ".worktrees",
        ".backups",
    }

    unexpected = sorted(
        entry.name
        for entry in ROOT.iterdir()
        if (
            entry.is_dir()
            and entry.name not in allowed_directories
        )
        or (
            entry.is_file()
            and entry.name not in allowed_files
            and not entry.name.endswith(".log")
            and not entry.name.endswith(".pyd")
        )
    )
    assert unexpected == []


def test_temporary_storage_is_owned_by_main_system() -> None:
    contract = _load_json(ROOT / "main-system" / "config" / "tool-runtime-contract.json")
    temporary = contract["temporary_storage"]
    assert temporary["root"] == "main-system/runtime/temp"
    assert temporary["tool_root_template"] == (
        "main-system/runtime/temp/tools/{tool_id}"
    )
    assert temporary["shared_layer_root"] == (
        "main-system/runtime/temp/shared-layer"
    )
    assert temporary["cleanup_owner"] == "main-system-internal-cleanup"

    legacy_temp_roots = [
        path
        for path in ROOT.glob("*/runtime/temp")
        if path != ROOT / "main-system" / "runtime" / "temp"
    ]
    assert legacy_temp_roots == []
    pytest_config = (ROOT / "pytest.ini").read_text("utf-8")
    assert "main-system/runtime/temp/development/pytest-cache" in pytest_config
    assert "global-cleaner" not in pytest_config


def test_development_tool_configuration_is_owned_by_main_system() -> None:
    development_tools = ROOT / "main-system" / "development-tools"
    assert (development_tools / "continue" / "agents" / "new-config.yaml").is_file()
    assert (
        development_tools
        / "continue"
        / "rules"
        / "globalcleanerbestpractices.md"
    ).is_file()
    assert (development_tools / "qodo" / "agents").is_dir()
    assert (development_tools / "qodo" / "workflows").is_dir()
    assert not any((ROOT / name).exists() for name in (".continue", ".qodo"))


def test_special_unpacked_tools_satisfy_runtime_contract() -> None:
    contract = _load_json(ROOT / "main-system" / "config" / "tool-runtime-contract.json")
    required = contract["independent_tool_runtime"]["special_unpacked_requires"]
    special_tools = 0
    for _tool_id, manifest_path in NON_GOVERNANCE_CASES:
        manifest = _load_json(manifest_path)
        distribution = manifest.get("distribution")
        if not isinstance(distribution, dict) or distribution.get("mode") != "special-unpackaged":
            continue
        assert distribution.get("package") == required["package"]
        assert manifest["request_channel"]["model"] == required["request_model"]
        assert (
            manifest["request_channel"]["direct_instruction"]
            == required["direct_instruction"]
        )
        special_tools += 1
    assert special_tools >= 2



########################################################################
