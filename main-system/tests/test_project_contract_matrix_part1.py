"""Split from consolidated test_main_system.py (main-system/tests/test_project_contract_matrix.py)."""
from __future__ import annotations

import _main_system_test_support as _support  # noqa: F401
from _main_system_test_support import (ROOT, _read_text_cached, _parse_python_cached,
    EXPECTED_TOOL_IDS, GOVERNANCE_TOOL_ID, SIBLING_IMPORT_ROOTS,
    MANIFEST_PATHS, TOOL_CASES, NON_GOVERNANCE_CASES,
    TOOL_ID_PATTERN, RUNTIME_CHANNEL_DATABASES, BACKGROUND_PROCESS_CALLS,
    ENVIRONMENT_NAME, NORMAL_ISOLATED_TOOLS, VISIBLE_PROCESS_CALLS)
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

import ast
import json
import re
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]







@cache
def _owned_python_files_cached(root_str: str) -> tuple[Path, ...]:
    tool_root = Path(root_str)
    source_root = tool_root / "src"
    scan_root = source_root if source_root.is_dir() else tool_root
    excluded = {"__pycache__", "build", "data", "dist", "runtime"}
    # Exclude subdirectories that have their own manifest.json (sub-tools)
    sub_tool_dirs = {
        d for d in scan_root.iterdir()
        if d.is_dir() and (d / "manifest.json").is_file()
    }
    return tuple(sorted(
        path
        for path in scan_root.rglob("*.py")
        if not excluded.intersection(path.relative_to(scan_root).parts)
        and not any(path.is_relative_to(d) for d in sub_tool_dirs)
    ))


def _owned_python_files(tool_root: Path) -> list[Path]:
    return list(_owned_python_files_cached(str(tool_root)))


def _assert_safe_relative_file(tool_root: Path, raw_path: str) -> Path:
    assert raw_path
    candidate = (tool_root / raw_path).resolve()
    candidate.relative_to(tool_root.resolve())
    assert candidate.is_file(), candidate
    return candidate


@pytest.mark.parametrize(("folder_name", "manifest_path"), TOOL_CASES)
def test_manifest_identity_matches_owned_folder_or_declared_companion(
    folder_name: str,
    manifest_path: Path,
) -> None:
    manifest = _load_json(manifest_path)
    assert manifest["id"] == folder_name
    # A278/A280: independent tools live under "Standalone tools/".
    # Direct children of ROOT (depth-1) and direct children of
    # "Standalone tools/" (depth-2) are top-level tools.  Companion
    # tools are nested at depth-3 under a tool root.
    parent = manifest_path.parent
    grandparent = parent.parent
    is_standalone_tool = (
        parent.parent.name == "Standalone tools"
        and grandparent.parent == ROOT
    )
    if parent.parent != ROOT and not is_standalone_tool:
        assert (
            manifest.get("main_system_independent_tool") is True
            or manifest.get("companion_tool") is True
        )
        host_manifest = _load_json(manifest_path.parents[1] / "manifest.json")
        assert manifest["host_tool_id"] == host_manifest["id"]
    assert TOOL_ID_PATTERN.fullmatch(folder_name)
    assert manifest.get("enabled") is True
    assert manifest.get("status") in {"running", "stopped"}


@pytest.mark.parametrize(("tool_id", "manifest_path"), TOOL_CASES)
def test_every_tool_keeps_version_one(tool_id: str, manifest_path: Path) -> None:
    del tool_id
    manifest = _load_json(manifest_path)
    assert manifest.get("version") == "1.0.0"
    assert manifest.get("display_version") == "1.0"


def test_launcher_bootstrap_uses_the_current_governance_authority_version() -> None:
    source = (
        ROOT / "main-system" / "src-ui" / "main" / "governance-bootstrap.ts"
    ).read_text("utf-8")
    assert "function governanceAuthorityVersion" in source
    assert "authority_version: governanceAuthorityVersion(workspaceRoot)" in source


@pytest.mark.parametrize(("tool_id", "manifest_path"), TOOL_CASES)
def test_tool_locale_resolves_manifest_keys(tool_id: str, manifest_path: Path) -> None:
    manifest = _load_json(manifest_path)
    locale_path = manifest_path.parent / "locales" / "zh-TW.json"
    locale = _load_json(locale_path)
    for field in ("name_key", "description_key"):
        key = str(manifest.get(field) or "")
        assert key, f"{tool_id} missing {field}"
        assert isinstance(locale.get(key), str)
        assert str(locale[key]).strip()
    window = manifest.get("window")
    if isinstance(window, dict) and window.get("title_key"):
        title_key = str(window["title_key"])
        assert str(locale.get(title_key) or "").strip()


@pytest.mark.parametrize(("tool_id", "manifest_path"), TOOL_CASES)
def test_runtime_entry_is_owned_python_file(tool_id: str, manifest_path: Path) -> None:
    manifest = _load_json(manifest_path)
    runtime = manifest.get("runtime")
    assert isinstance(runtime, dict), tool_id
    assert runtime.get("type") == "python"
    assert runtime.get("workingDirectory") == "."
    entry = _assert_safe_relative_file(manifest_path.parent, str(runtime.get("entry") or ""))
    assert entry.suffix == ".py"


@pytest.mark.parametrize(("tool_id", "manifest_path"), TOOL_CASES)
def test_permissions_declare_owned_code_and_database_scope(
    tool_id: str,
    manifest_path: Path,
) -> None:
    permissions = _load_json(manifest_path).get("permissions")
    assert isinstance(permissions, dict)
    if tool_id == "xingcheng":
        assert permissions.get("code_scope") == "project-source-excluding-governance-rule"
        assert permissions.get("database_scope") == (
            "opaque-central-index-read-and-xingcheng-internal-read-write"
        )
        assert permissions.get("allow_modify") == [
            "Standalone tools/local-model/model-dialogue/xingcheng-excluding-permissions"
        ]
        assert {"governance-rule", "governance-permission-directory"}.issubset(
            set(permissions.get("deny", []))
        )
        return
    if tool_id == "star-chat":
        assert permissions.get("profile") == "local-model-platform-v1"
        assert permissions.get("business_permission_owner") == "xingcheng"
        assert permissions.get("settings_owner") == "xingcheng"
        assert permissions.get("database_scope") == (
            "all-project-databases-via-xingcheng-excluding-governance-rule"
        )
        return
    if tool_id == "investment-mobile":
        assert permissions.get("profile") == "ai-investment-manager-v1"
        assert permissions.get("business_permission_owner") == "ai-assistant"
        assert permissions.get("settings_owner") == "ai-assistant"
        assert permissions.get("code_scope") == "tool-root-only"
        assert permissions.get("database_scope") == "tool-database-only"
        assert (
            permissions.get("canonical_database_scope")
            == "xingcheng-shared-repository"
        )
        assert "direct-database-write" in permissions.get("deny", [])
        return
    assert permissions.get("code_scope") == "tool-root-only"
    expected_database_scope = (
        "none" if tool_id == GOVERNANCE_TOOL_ID else "tool-database-only"
    )
    assert permissions.get("database_scope") == expected_database_scope
    if tool_id in NORMAL_ISOLATED_TOOLS:
        assert "own-data" in permissions.get("allow_modify", [])
        assert {"main-program", "other-tools"}.issubset(
            set(permissions.get("deny", []))
        )


@pytest.mark.parametrize(("tool_id", "manifest_path"), TOOL_CASES)
def test_manifest_capabilities_use_governance_approved_labels(
    tool_id: str,
    manifest_path: Path,
) -> None:
    capabilities = _load_json(manifest_path).get("capabilities")
    assert isinstance(capabilities, dict), tool_id
    approved = set(code_rule_directory_snapshot().approved_capability_names)
    assert set(capabilities).issubset(approved)


@pytest.mark.parametrize(("tool_id", "manifest_path"), NON_GOVERNANCE_CASES)
def test_request_channel_is_governed_and_fail_closed(
    tool_id: str,
    manifest_path: Path,
) -> None:
    manifest = _load_json(manifest_path)
    channel = manifest.get("request_channel")
    assert isinstance(channel, dict), tool_id
    assert channel.get("model") == "governance-authenticated-shared-layer"
    assert channel.get("direct_instruction") == "PERMISSION_DENIED"
    runtime_entry = _assert_safe_relative_file(
        manifest_path.parent,
        str(channel.get("runtime_entry") or ""),
    )
    assert runtime_entry.name == "channel_runtime.py"


@pytest.mark.parametrize(("tool_id", "manifest_path"), NON_GOVERNANCE_CASES)
def test_launch_configuration_has_one_coherent_runtime_strategy(
    tool_id: str,
    manifest_path: Path,
) -> None:
    manifest = _load_json(manifest_path)
    launch = manifest.get("launch")
    assert isinstance(launch, dict), tool_id
    if launch.get("mode") == "dual-runtime":
        assert launch.get("selection") == "automatic"
        runtimes = launch.get("runtimes")
        assert isinstance(runtimes, list) and len(runtimes) == len(set(runtimes))
        assert "executable" in runtimes
        assert any(str(mode).startswith("governed-source") for mode in runtimes)
        executable = manifest.get("executable")
        assert isinstance(executable, dict)
        executable_path = (manifest_path.parent / str(executable.get("path") or "")).resolve()
        executable_path.relative_to(manifest_path.parent.resolve())
        assert executable_path.suffix.casefold() == ".exe"
    else:
        assert launch.get("primary") == "governed-source-channel"
        distribution = manifest.get("distribution")
        assert isinstance(distribution, dict)
        assert distribution.get("mode") == "special-unpackaged"
        assert distribution.get("package") is False
    assert launch.get("background") == "governed-source-channel"


@pytest.mark.parametrize(("tool_id", "manifest_path"), TOOL_CASES)
def test_environment_allowlist_is_explicit_and_well_formed(
    tool_id: str,
    manifest_path: Path,
) -> None:
    environment = _load_json(manifest_path).get("environment")
    assert isinstance(environment, dict), tool_id
    allow = environment.get("allow")
    assert isinstance(allow, list)
    assert len(allow) == len(set(allow))
    assert all(isinstance(name, str) and ENVIRONMENT_NAME.fullmatch(name) for name in allow)


@pytest.mark.parametrize(("tool_id", "manifest_path"), TOOL_CASES)
def test_all_owned_python_sources_parse(tool_id: str, manifest_path: Path) -> None:
    files = _owned_python_files(manifest_path.parent)
    assert files, tool_id
    for source_path in files:
        _parse_python_cached(str(source_path))


@pytest.mark.parametrize(("tool_id", "manifest_path"), TOOL_CASES)
def test_owned_python_sources_do_not_import_sibling_implementations(
    tool_id: str,
    manifest_path: Path,
) -> None:
    own_root = tool_id.replace("-", "_")
    forbidden = SIBLING_IMPORT_ROOTS - {own_root, GOVERNANCE_TOOL_ID}
    if tool_id == "xingcheng":
        # Model dialogue is a physical child of xingcheng and shares its
        # implementation boundary even though main-system exposes a separate
        # star-chat lifecycle identity.
        forbidden.discard("star_chat")
    if tool_id == "model-dialogue":
        # model-dialogue's runtime entry IS star-chat; it legitimately imports
        # star_chat.application.service as its own implementation.
        forbidden.discard("star_chat")
    violations: list[str] = []
    for source_path in _owned_python_files(manifest_path.parent):
        tree = _parse_python_cached(str(source_path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                imported_root = module.split(".", 1)[0]
                if imported_root in forbidden:
                    violations.append(
                        f"{source_path.relative_to(ROOT)} imports {module}"
                    )
    assert not violations, "\n".join(violations)


def test_project_manifest_inventory_is_complete_and_unique() -> None:
    discovered_ids = [_load_json(path)["id"] for path in MANIFEST_PATHS]
    assert set(discovered_ids) == EXPECTED_TOOL_IDS
    assert len(discovered_ids) == len(set(discovered_ids))


def test_runtime_channel_databases_are_ignored_and_untracked() -> None:
    runtime_files = {
        f"{database_path}{suffix}"
        for database_path in RUNTIME_CHANNEL_DATABASES
        for suffix in ("", "-shm", "-wal")
    }
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "--stdin"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        input="\n".join(sorted(runtime_files)) + "\n",
    )
    # git check-ignore --no-index outputs quoted paths with literal \r on Windows
    ignored_lines = {
        line.strip().strip('"').removesuffix('\\r')
        for line in ignored.stdout.splitlines()
    }
    not_ignored = sorted(
        runtime_files - ignored_lines
    )
    assert not not_ignored, (
        "runtime databases are not ignored:\n" + "\n".join(not_ignored)
    )

    tracked = subprocess.run(
        ["git", "ls-files", "--", *sorted(runtime_files)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not tracked.stdout.strip(), (
        "runtime databases must not be tracked:\n" + tracked.stdout
    )


def test_windows_background_processes_cannot_open_console_windows() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z", "--", "*.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    source_paths = [
        ROOT / raw_path.decode("utf-8", errors="surrogateescape")
        for raw_path in tracked.stdout.split(b"\0")
        if raw_path
    ]
    violations: list[str] = []

    for source_path in source_paths:
        relative_path = source_path.relative_to(ROOT).as_posix()
        if "/tests/" in f"/{relative_path}" or relative_path.startswith("tests/"):
            continue
        if relative_path.endswith("scripts/visual_smoke.py"):
            continue
        source_text = _read_text_cached(str(source_path))
        if "subprocess" not in source_text:
            continue
        tree = _parse_python_cached(str(source_path))
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            call_name = ast.unparse(node.func)
            if call_name not in BACKGROUND_PROCESS_CALLS:
                continue
            owner = "<module>"
            parent: ast.AST = node
            while parent in parents:
                parent = parents[parent]
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = parent.name
                    break
            first_argument = ast.unparse(node.args[0]) if node.args else ""
            if (relative_path, owner, first_argument) in VISIBLE_PROCESS_CALLS:
                continue
            has_no_window_policy = any(
                keyword.arg == "creationflags"
                or (
                    keyword.arg is None
                    and (
                        "subprocess_kwargs" in ast.unparse(keyword.value)
                        or "CREATE_NO_WINDOW" in ast.unparse(keyword.value)
                    )
                )
                for keyword in node.keywords
            )
            if not has_no_window_policy or "CREATE_NO_WINDOW" not in source_text:
                violations.append(f"{relative_path}:{node.lineno} ({owner})")

    assert not violations, (
        "background subprocesses must apply the Windows no-window policy:\n"
        + "\n".join(violations)
    )
