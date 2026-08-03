from __future__ import annotations

import ast
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOL_IDS = {
    "ai-assistant",
    "ai-collaboration",
    "file-sorter",
    "global-cleaner",
    "governance_rule",
    "investment-mobile",
    "local-ai",
    "star-chat",
    "system-rescue",
    "vaultly",
}
GOVERNANCE_TOOL_ID = "governance_rule"
NORMAL_ISOLATED_TOOLS = {
    "ai-assistant",
    "ai-collaboration",
    "file-sorter",
    "investment-mobile",
    "local-ai",
    "star-chat",
    "vaultly",
}
SIBLING_IMPORT_ROOTS = {tool_id.replace("-", "_") for tool_id in EXPECTED_TOOL_IDS}
ENVIRONMENT_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")
TOOL_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
RUNTIME_CHANNEL_DATABASES = {
    "shared-layer/data/ai-channel.sqlite3",
    "shared-layer/data/system-channel.sqlite3",
}
BACKGROUND_PROCESS_CALLS = {
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.check_output",
    "subprocess.check_call",
    "subprocess.call",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
}
VISIBLE_PROCESS_CALLS = {
    (
        "main-system/src-core/tasks/toolbox_service.py",
        "_launch_source_ui",
        "str(electron)",
    ),
    (
        "main-system/src-core/tasks/toolbox_service.py",
        "_activate_existing_tool_window",
        "str(executable_file)",
    ),
    (
        "main-system/src-core/tasks/toolbox_service.py",
        "start_tool",
        "str(executable_file)",
    ),
    (
        "system-rescue/src/backend/services/system_rescue/integration/platform_packager.py",
        "restart_packaged_executable",
        "[str(executable_file.resolve())]",
    ),
}

from governance_rule.code_rule_directory import (  # noqa: E402
    code_rule_directory_snapshot,
)


def _manifest_paths() -> list[Path]:
    paths = list(
        path
        for path in ROOT.glob("*/manifest.json")
        if json.loads(path.read_text("utf-8")).get("id") in EXPECTED_TOOL_IDS
    )
    paths.extend(
        path
        for path in ROOT.glob("*/*/manifest.json")
        if json.loads(path.read_text("utf-8")).get("main_system_independent_tool")
        is True
    )
    return sorted(paths)


MANIFEST_PATHS = _manifest_paths()
TOOL_CASES = [
    (
        str(json.loads(path.read_text("utf-8")).get("id") or path.parent.name),
        path,
    )
    for path in MANIFEST_PATHS
]
NON_GOVERNANCE_CASES = [
    case for case in TOOL_CASES if case[0] != GOVERNANCE_TOOL_ID
]


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text("utf-8"))
    assert isinstance(payload, dict)
    return payload


def _owned_python_files(tool_root: Path) -> list[Path]:
    source_root = tool_root / "src"
    scan_root = source_root if source_root.is_dir() else tool_root
    excluded = {"__pycache__", "build", "data", "dist", "runtime"}
    return sorted(
        path
        for path in scan_root.rglob("*.py")
        if not excluded.intersection(path.relative_to(scan_root).parts)
    )


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
    if manifest_path.parent.parent != ROOT:
        assert manifest["main_system_independent_tool"] is True
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
    if tool_id == "local-ai":
        assert permissions.get("code_scope") == "project-source-excluding-governance-rule"
        assert permissions.get("database_scope") == (
            "opaque-central-index-read-and-local-ai-internal-read-write"
        )
        assert permissions.get("allow_modify") == [
            "local-model/local-ai-excluding-permissions"
        ]
        assert {"governance-rule", "governance-permission-directory"}.issubset(
            set(permissions.get("deny", []))
        )
        return
    if tool_id == "star-chat":
        assert permissions.get("profile") == "local-model-platform-v1"
        assert permissions.get("business_permission_owner") == "local-ai"
        assert permissions.get("settings_owner") == "local-ai"
        assert permissions.get("database_scope") == (
            "all-project-databases-via-local-ai-excluding-governance-rule"
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
            == "ai-assistant-shared-repository"
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
        ast.parse(source_path.read_text("utf-8"), filename=str(source_path))


@pytest.mark.parametrize(("tool_id", "manifest_path"), TOOL_CASES)
def test_owned_python_sources_do_not_import_sibling_implementations(
    tool_id: str,
    manifest_path: Path,
) -> None:
    own_root = tool_id.replace("-", "_")
    forbidden = SIBLING_IMPORT_ROOTS - {own_root, GOVERNANCE_TOOL_ID}
    if tool_id == "local-ai":
        # Model dialogue is a physical child of local-ai and shares its
        # implementation boundary even though main-system exposes a separate
        # star-chat lifecycle identity.
        forbidden.discard("star_chat")
    violations: list[str] = []
    for source_path in _owned_python_files(manifest_path.parent):
        tree = ast.parse(source_path.read_text("utf-8"), filename=str(source_path))
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
    for runtime_file in sorted(runtime_files):
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", runtime_file],
            cwd=ROOT,
            check=False,
        )
        assert ignored.returncode == 0, f"runtime database is not ignored: {runtime_file}"

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
        source_text = source_path.read_text("utf-8")
        tree = ast.parse(source_text, filename=str(source_path))
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


def test_launcher_entrypoints_delegate_to_no_window_hosts() -> None:
    for filename in ("run-interface.bat", "啟動主程式.bat", "自動化工具.bat"):
        source = (ROOT / "main-system" / filename).read_text("utf-8")
        assert "wscript.exe //B //Nologo" in source
        assert "powershell.exe" not in source.casefold()

    vbs_source = (ROOT / "main-system" / "start-hidden.vbs").read_text("utf-8")
    assert 'shell.Run "powershell.exe' in vbs_source
    assert '", 0, False' in vbs_source

    launcher_source = (
        ROOT / "main-system" / "launcher" / "src" / "GPTBridgeLauncher.cs"
    ).read_text("utf-8")
    assert "CreateNoWindow = true" in launcher_source
    assert "WindowStyle = ProcessWindowStyle.Hidden" in launcher_source


def test_manifest_test_targets_resolve_to_real_test_files() -> None:
    checked = 0
    for manifest_path in MANIFEST_PATHS:
        manifest = _load_json(manifest_path)
        for raw_target in manifest.get("test_targets", []):
            candidates = [
                manifest_path.parent / str(raw_target),
                ROOT / "main-system" / str(raw_target),
            ]
            assert any(candidate.is_file() for candidate in candidates), (
                manifest["id"],
                raw_target,
            )
            checked += 1
    assert checked >= 20


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
    allowed_directories = governed_modules | {".git", "main-system", "shared-layer"}
    allowed_files = {".gitignore", "pytest.ini"}

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
        )
    )
    assert unexpected == []


def test_temporary_storage_is_owned_by_global_cleaner() -> None:
    contract = _load_json(ROOT / "main-system" / "config" / "tool-runtime-contract.json")
    temporary = contract["temporary_storage"]
    assert temporary["root"] == "global-cleaner/runtime/temp"
    assert temporary["tool_root_template"] == (
        "global-cleaner/runtime/temp/tools/{tool_id}"
    )
    assert temporary["shared_layer_root"] == (
        "global-cleaner/runtime/temp/shared-layer"
    )
    assert temporary["cleanup_owner"] == "global-cleaner"

    legacy_temp_roots = [
        path
        for path in ROOT.glob("*/runtime/temp")
        if path != ROOT / "global-cleaner" / "runtime" / "temp"
    ]
    assert legacy_temp_roots == []
    pytest_config = (ROOT / "pytest.ini").read_text("utf-8")
    assert "global-cleaner/runtime/temp/development/pytest-cache" in pytest_config


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
    assert not any((ROOT / name).exists() for name in (".continue", ".devin", ".qodo"))


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
