"""main-system consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

def _get_main_project_root() -> Path:
    """Get the main project root (E:/GPTBridge) regardless of worktree."""
    # Try to get from git common dir
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, check=True, timeout=5
        )
        git_common_dir = Path(result.stdout.strip())
        # Main project root is parent of .git
        return git_common_dir.parent
    except Exception:
        pass
    # Fallback: check if we're in a known worktree location
    current = Path(__file__).resolve().parents[2]
    if ".kilo\\worktrees" in str(current) or ".worktrees" in str(current):
        return Path("E:/GPTBridge")
    return current


_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --


import ast
import json
import re
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MAIN_PROJECT_ROOT = _get_main_project_root()
LOCAL_MODEL_ROOT = ROOT / "Standalone tools" / "local-model"

@cache
def _read_text_cached(path_str: str) -> str:
    """Session-cached file text -- parametrized cases re-read the same
    manifests and sources dozens of times; caching the immutable text
    keeps each call returning an equivalent but fresh parse."""
    return Path(path_str).read_text("utf-8")


@cache
def _parse_python_cached(path_str: str) -> ast.AST:
    """Session-cached AST -- several tests walk the same owned sources;
    tests only read the tree, so sharing it is safe."""
    return ast.parse(_read_text_cached(path_str), filename=path_str)


EXPECTED_TOOL_IDS = {
    "ai-assistant",
    "ai-collaboration",
    "file-sorter",
    "governance_rule",
    "investment-mobile",
    "model-dialogue",
    "xingcheng",
    "star-chat",
    "vaultly",
}
GOVERNANCE_TOOL_ID = "governance_rule"
NORMAL_ISOLATED_TOOLS = {
    "ai-assistant",
    "ai-collaboration",
    "file-sorter",
    "investment-mobile",
    "xingcheng",
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
        "main-system/src-core/tasks/platform_packager.py",
        "restart_packaged_executable",
        "[str(executable_file.resolve())]",
    ),
}


def _manifest_paths() -> list[Path]:
    def _is_valid_path(path: Path) -> bool:
        """Exclude worktree and hidden directories."""
        parts = path.parts
        return not any(part.startswith(".") or part == "worktrees" for part in parts)

    # Use MAIN_PROJECT_ROOT for manifest discovery (structural test)
    search_root = MAIN_PROJECT_ROOT
    paths = list(
        path
        for path in search_root.glob("*/manifest.json")
        if _is_valid_path(path)
        and json.loads(_read_text_cached(str(path))).get("id") in EXPECTED_TOOL_IDS
    )
    paths.extend(
        path
        for path in search_root.glob("Standalone tools/*/manifest.json")
        if _is_valid_path(path)
        and json.loads(_read_text_cached(str(path))).get("id") in EXPECTED_TOOL_IDS
    )
    paths.extend(
        path
        for path in search_root.glob("Standalone tools/*/*/manifest.json")
        if _is_valid_path(path)
        and (
            json.loads(_read_text_cached(str(path))).get("main_system_independent_tool")
            is True
            or json.loads(_read_text_cached(str(path))).get("companion_tool") is True
        )
    )
    paths.extend(
        path
        for path in search_root.glob("Standalone tools/*/*/*/manifest.json")
        if _is_valid_path(path)
        and (
            json.loads(_read_text_cached(str(path))).get("main_system_independent_tool")
            is True
            or json.loads(_read_text_cached(str(path))).get("companion_tool") is True
        )
    )
    paths.extend(
        path
        for path in search_root.glob("Standalone tools/*/*/*/*/manifest.json")
        if _is_valid_path(path)
        and (
            json.loads(_read_text_cached(str(path))).get("main_system_independent_tool")
            is True
            or json.loads(_read_text_cached(str(path))).get("companion_tool") is True
        )
    )
    return sorted(paths)


MANIFEST_PATHS = _manifest_paths()


TOOL_CASES = [
    (
        str(json.loads(_read_text_cached(str(path))).get("id") or path.parent.name),
        path,
    )
    for path in MANIFEST_PATHS
]


NON_GOVERNANCE_CASES = [
    case for case in TOOL_CASES if case[0] != GOVERNANCE_TOOL_ID
]