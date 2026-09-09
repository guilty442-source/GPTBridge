from __future__ import annotations

import json
import shutil
import stat as stat_module
from pathlib import Path
from typing import Any

from packager_base import (
    MAIN_SYSTEM_ROOT,
    PROJECT_ROOT,
    declared_source_exclusions,
)
from packager_metadata import (
    load_manifest,
    tool_id_from_directory,
)
from packager_runtime import package_copy_ignore


SRC_CORE_DIR = MAIN_SYSTEM_ROOT / "src-core"


def copy_runtime_source(
    tool_dir: Path,
    entry: Path,
    app_dir: Path,
    *,
    excluded_root_names: frozenset[str] = frozenset(),
) -> Path | None:
    try:
        relative_entry = entry.resolve().relative_to(tool_dir.resolve())
    except ValueError:
        return None
    if not relative_entry.parts:
        return None

    source_root = tool_dir / relative_entry.parts[0]
    if not source_root.is_dir():
        return None
    canonical_source_root = source_root.resolve()

    target_root = app_dir / relative_entry.parts[0]
    if target_root.exists():
        target_resolved = target_root.resolve()
        app_resolved = app_dir.resolve()
        if app_resolved != target_resolved and app_resolved not in target_resolved.parents:
            raise RuntimeError(f"Refusing to replace path outside app package: {target_root}")
        shutil.rmtree(target_root)

    def runtime_copy_ignore(directory: str, names: list[str]) -> set[str]:
        ignored = package_copy_ignore(directory, names)
        if Path(directory).resolve() == canonical_source_root:
            ignored.update(name for name in names if name in excluded_root_names)
        return ignored

    shutil.copytree(
        source_root,
        target_root,
        ignore=runtime_copy_ignore,
    )
    return target_root


def copy_backend_source_bundle(
    tool_id: str,
    tool_dir: Path,
    entry: Path,
    app_dir: Path,
) -> Path:
    manifest = load_manifest(tool_dir) or {}
    request_channel = manifest.get("request_channel")
    governed_channel = (
        isinstance(request_channel, dict)
        and request_channel.get("model") == "governance-authenticated-shared-layer"
    )
    if governed_channel:
        packaged_tool_dir = app_dir / "independent_tool" / tool_id
    else:
        core_target = app_dir / "src-core"
        if core_target.exists():
            shutil.rmtree(core_target)
        shutil.copytree(SRC_CORE_DIR, core_target, ignore=package_copy_ignore)
        packaged_tool_dir = app_dir / "platform_tools" / tool_id
    packaged_tool_dir.mkdir(parents=True, exist_ok=True)
    relative_entry = entry.resolve().relative_to(tool_dir.resolve())
    runtime_root_name = relative_entry.parts[0]
    excluded_paths = package_source_excluded_paths(tool_dir, entry)
    package_policy = manifest.get("package")
    generated_files = (
        package_policy.get("generated_files", {})
        if isinstance(package_policy, dict)
        else {}
    )
    if not isinstance(generated_files, dict):
        raise RuntimeError("package.generated_files must be an object")
    source_project_root = tool_dir.resolve().parent
    tool_relative_exclusions: list[Path] = []
    for excluded_path in excluded_paths:
        excluded_source = source_project_root / excluded_path
        relative = excluded_source.relative_to(tool_dir.resolve())
        if not relative.parts or relative.parts[0] != runtime_root_name:
            raise RuntimeError(
                f"Package exclusion is outside the runtime root: {excluded_path}"
            )
        if excluded_source.exists() or excluded_source.is_symlink():
            excluded_stat = excluded_source.lstat()
            attributes = int(
                getattr(excluded_stat, "st_file_attributes", 0) or 0
            )
            if (
                stat_module.S_ISLNK(excluded_stat.st_mode)
                or bool(attributes & 0x400)
                or not stat_module.S_ISREG(excluded_stat.st_mode)
            ):
                raise RuntimeError(
                    "Excluded package source must be a regular, non-reparse "
                    f"file: {excluded_source}"
                )
        tool_relative_exclusions.append(relative)
    runtime_path = copy_runtime_source(
        tool_dir,
        entry,
        packaged_tool_dir,
    )
    if runtime_path is None:
        raise RuntimeError(
            f"Tool runtime entry must be inside its tool directory: {entry}"
        )
    excluded_set = {path.as_posix() for path in tool_relative_exclusions}
    for relative in tool_relative_exclusions:
        packaged_path = packaged_tool_dir / relative
        if packaged_path.exists() or packaged_path.is_symlink():
            if packaged_path.is_dir() and not packaged_path.is_symlink():
                raise RuntimeError(
                    f"Excluded package source became a directory: {packaged_path}"
                )
            packaged_path.unlink()
    for raw_relative, default_value in generated_files.items():
        relative = Path(str(raw_relative))
        canonical = relative.as_posix()
        if canonical not in excluded_set or relative.is_absolute() or ".." in relative.parts:
            raise RuntimeError(
                "Generated package files must also be declared in source_excludes"
            )
        generated_path = packaged_tool_dir / relative
        generated_path.parent.mkdir(parents=True, exist_ok=True)
        generated_path.write_text(
            json.dumps(default_value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    shutil.copy2(tool_dir / "manifest.json", packaged_tool_dir / "manifest.json")
    return runtime_path


def package_source_roots(tool_dir: Path, entry: Path) -> list[str]:
    relative_entry = entry.resolve().relative_to(tool_dir.resolve())
    runtime_root = tool_dir / relative_entry.parts[0]
    candidates = [
        tool_dir / "manifest.json",
        tool_dir / "README.md",
        runtime_root,
        MAIN_SYSTEM_ROOT / "config" / "tool-runtime-contract.json",
    ]
    request_channel = (load_manifest(tool_dir) or {}).get("request_channel")
    if (
        isinstance(request_channel, dict)
        and request_channel.get("model") == "governance-authenticated-shared-layer"
    ):
        candidates.append(PROJECT_ROOT / "shared-layer" / "src" / "shared_layer")
    roots: list[str] = []
    for candidate in candidates:
        if candidate.exists():
            roots.append(candidate.resolve().relative_to(PROJECT_ROOT).as_posix())
    return roots


def package_source_excluded_paths(tool_dir: Path, entry: Path) -> list[str]:
    del entry
    tool_id = tool_id_from_directory(tool_dir)
    tool_root = tool_dir.resolve()
    source_project_root = tool_root.parent
    excluded_paths = declared_source_exclusions(source_project_root, tool_id)
    if excluded_paths is None:
        raise RuntimeError(
            f"Invalid package.source_excludes declaration for {tool_id}"
        )
    for relative_path in excluded_paths:
        candidate = (source_project_root / relative_path).resolve()
        try:
            candidate.relative_to(tool_root)
        except ValueError as error:
            raise RuntimeError(
                f"Package exclusion escaped tool root: {relative_path}"
            ) from error
    return sorted(excluded_paths)
