from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_CORE = PROJECT_ROOT / "src-core"
TOOLS_DIR = PROJECT_ROOT / "platform_tools"
DATABASE_PATH = PROJECT_ROOT / "runtime" / "state" / "gptbridge.sqlite3"
TOOLBOX_REGISTRY_PATH = (
    PROJECT_ROOT / "src-ui" / "renderer" / "ui" / "toolbox" / "tools" / "registry.ts"
)
GENERIC_LAYER_SCAN_PATHS = (
    PROJECT_ROOT / "src-core" / "ipc" / "server.py",
    PROJECT_ROOT / "src-core" / "ipc" / "handlers.py",
    PROJECT_ROOT / "src-core" / "main.py",
    PROJECT_ROOT / "src-core" / "modes" / "mode_manager.py",
    PROJECT_ROOT / "src-core" / "settings" / "service.py",
    PROJECT_ROOT / "src-ui" / "main" / "index.ts",
)
PLATFORM_TOOL_FORBIDDEN_IMPORTS = (
    "../../../_shared",
    "../../_shared",
    "platform_tools/_shared",
    "platform_tools\\_shared",
    "from managers",
    "import managers",
    "from utils",
    "import utils",
    "from tasks.",
    "import tasks.",
    "src-core",
    "src-ui",
)


def fail(issues: list[str], message: str) -> None:
    issues.append(message)


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def audit_tool_modules(issues: list[str]) -> set[str]:
    if not TOOLS_DIR.exists():
        fail(issues, "platform_tools parent directory is missing")
        return set()

    manifest_ids: set[str] = set()
    for tool_dir in sorted(
        (
            path
            for path in TOOLS_DIR.iterdir()
            if path.is_dir() and not path.name.startswith("_")
        ),
        key=lambda path: path.name.lower(),
    ):
        tool_id = tool_dir.name
        manifest_path = tool_dir / "manifest.json"
        if not manifest_path.exists():
            fail(issues, f"{tool_id} manifest.json is missing")
            continue

        try:
            manifest = load_json(manifest_path)
        except Exception as exc:
            fail(issues, f"{tool_id} manifest.json is invalid: {exc}")
            continue

        manifest_id = str(manifest.get("id", "")).strip()
        if not manifest_id:
            fail(issues, f"{tool_id} manifest id is missing")
            continue
        if manifest_id in manifest_ids:
            fail(issues, f"duplicate manifest id: {manifest_id}")
            continue
        manifest_ids.add(manifest_id)
        if manifest_id != tool_id:
            fail(issues, f"{tool_id} manifest id mismatch: {manifest_id}")

        entry = str(manifest.get("entry", "")).strip()
        if not entry:
            fail(issues, f"{tool_id} manifest entry is missing")
            continue

        entry_path = PROJECT_ROOT / entry
        if entry_path.suffix == "":
            entry_path = entry_path.with_suffix(".py")
        if not entry_path.exists():
            fail(issues, f"{tool_id} entry file is missing: {entry_path}")

        window_config = manifest.get("window")
        if not isinstance(window_config, dict):
            fail(issues, f"{tool_id} manifest window config is missing")

        test_targets = manifest.get("test_targets", [])
        if test_targets is not None and not isinstance(test_targets, list):
            fail(issues, f"{tool_id} manifest test_targets must be a list")
        for test_target in test_targets or []:
            test_path = PROJECT_ROOT / str(test_target)
            if not test_path.exists():
                fail(issues, f"{tool_id} test target is missing: {test_target}")

    return manifest_ids


async def sync_tool_database() -> None:
    sys.path.insert(0, str(SRC_CORE))
    from tasks.toolbox_service import ToolboxService

    service = ToolboxService(PROJECT_ROOT)
    await service.list_tools()


def audit_tool_database(issues: list[str], manifest_ids: set[str]) -> None:
    if not DATABASE_PATH.exists():
        fail(issues, f"tool database is missing: {DATABASE_PATH}")
        return

    with sqlite3.connect(DATABASE_PATH) as connection:
        rows = connection.execute(
            "SELECT id, entry, code_path FROM toolbox_tools ORDER BY id"
        ).fetchall()

    database_ids = {str(row[0]) for row in rows}
    if database_ids != manifest_ids:
        fail(
            issues,
            f"tool database ids do not match manifests: db={sorted(database_ids)}, manifests={sorted(manifest_ids)}",
        )

    for tool_id, entry, code_path in rows:
        if not entry:
            fail(issues, f"{tool_id} database entry is empty")
        if not code_path or not Path(code_path).exists():
            fail(issues, f"{tool_id} database code_path is missing: {code_path}")


def audit_mother_core_isolation(_issues: list[str]) -> None:
    # Platform-tool isolation is enforced by governance:check (G-115).
    # This structure audit stays focused on folder, manifest, and database wiring
    # so it does not duplicate or drift from the blocking governance checker.
    return None


def audit_project_size_source(issues: list[str]) -> None:
    try:
        source = TOOLBOX_REGISTRY_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        fail(issues, f"toolbox registry cannot be read: {exc}")
        return

    if "projectSizeBytes" in source:
        fail(
            issues,
            "toolbox registry must not hardcode projectSizeBytes; use runtime platform tool size sync",
        )


def iter_source_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if not path.exists():
        return []
    return [
        item
        for item in path.rglob("*")
        if item.is_file()
        and item.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".md"}
    ]


def audit_no_legacy_child_tool_output(issues: list[str]) -> None:
    legacy_root = PROJECT_ROOT / "release" / "child-tools"
    if legacy_root.exists():
        fail(issues, "legacy release/child-tools output must not remain in the project")


def audit_no_root_import_shims(issues: list[str]) -> None:
    for folder_name in ("managers", "tasks"):
        shim_root = PROJECT_ROOT / folder_name
        if shim_root.exists():
            fail(
                issues,
                f"root-level {folder_name}/ import shim is forbidden; use src-core/{folder_name}/ explicitly",
            )


def audit_no_shared_platform_tool_code(issues: list[str], manifest_ids: set[str]) -> None:
    shared_root = TOOLS_DIR / "_shared"
    if shared_root.exists():
        fail(issues, "platform_tools/_shared is forbidden; each application must own its code")

    for tool_id in sorted(manifest_ids):
        legacy_core_task = SRC_CORE / "tasks" / tool_id
        if legacy_core_task.exists():
            fail(
                issues,
                f"{legacy_core_task.relative_to(PROJECT_ROOT).as_posix()} is forbidden; tool runtime code must stay in platform_tools/{tool_id}",
            )


def audit_platform_tools_do_not_import_shared_code(issues: list[str]) -> None:
    if not TOOLS_DIR.exists():
        return

    for tool_dir in sorted(TOOLS_DIR.iterdir(), key=lambda path: path.name.lower()):
        if not tool_dir.is_dir() or tool_dir.name.startswith("_"):
            continue
        for source_path in iter_source_files(tool_dir):
            if source_path.suffix.lower() not in {".py", ".ts", ".tsx", ".js"}:
                continue
            try:
                source = source_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                fail(issues, f"{source_path.relative_to(PROJECT_ROOT)} is not valid UTF-8: {exc}")
                continue

            offenders = [
                token
                for token in PLATFORM_TOOL_FORBIDDEN_IMPORTS
                if token in source
            ]
            if offenders:
                relative = source_path.relative_to(PROJECT_ROOT).as_posix()
                fail(
                    issues,
                    f"{relative} imports shared or mother-system code: {sorted(set(offenders))}",
                )


def audit_generic_layers_do_not_hardcode_tools(
    issues: list[str],
    manifest_ids: set[str],
) -> None:
    hardcoded_terms = sorted(manifest_ids)
    hardcoded_terms.extend(
        [
            "settings_cleanup_garbage",
            "readChildToolWindowConfig",
            "PlatformToolWindowApp",
        ]
    )

    for scan_path in GENERIC_LAYER_SCAN_PATHS:
        for source_path in iter_source_files(scan_path):
            try:
                source = source_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                fail(issues, f"{source_path.relative_to(PROJECT_ROOT)} is not valid UTF-8: {exc}")
                continue

            offenders = [
                term
                for term in hardcoded_terms
                if term and term in source
            ]
            if offenders:
                relative = source_path.relative_to(PROJECT_ROOT).as_posix()
                fail(
                    issues,
                    f"{relative} hardcodes platform tool terms: {sorted(set(offenders))}",
                )


async def main() -> int:
    issues: list[str] = []
    manifest_ids = audit_tool_modules(issues)
    await sync_tool_database()
    audit_tool_database(issues, manifest_ids)
    audit_mother_core_isolation(issues)
    audit_project_size_source(issues)
    audit_no_legacy_child_tool_output(issues)
    audit_no_root_import_shims(issues)
    audit_no_shared_platform_tool_code(issues, manifest_ids)
    audit_platform_tools_do_not_import_shared_code(issues)
    audit_generic_layers_do_not_hardcode_tools(issues, manifest_ids)

    if issues:
        print("Project structure audit failed:")
        for issue in issues:
            print(f"- {issue}")
        return 1

    print("Project structure audit passed:")
    print(f"- platform tools: {sorted(manifest_ids)}")
    print(f"- database: {DATABASE_PATH}")
    print("- platform-tool isolation: covered by governance:check")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
