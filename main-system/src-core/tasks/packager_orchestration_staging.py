"""Packager staging helpers (A185 split).

Contains the _stage_package function extracted from _package_tool_locked.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from packager_base import (
    PROJECT_ROOT,
    SOURCE_IGNORED_DIRECTORY_NAMES,
    collect_file_hashes,
    snapshot_digest,
    verify_packaged_app,
)
from packager_bundle import (
    copy_backend_source_bundle,
    package_source_excluded_paths,
    package_source_roots,
)
from packager_renderer import copy_app_templates
from packager_runtime import (
    copy_electron_runtime,
    copy_portable_python_runtime,
    validate_staged_python_runtime,
)

from packager_orchestration_manifests import (
    _resolve_backend_entry,
    _write_app_manifests,
    _write_package_metadata,
)


def _stage_package(
    tool_id: str,
    tool_dir: Path,
    entry: Path,
    manifest: dict[str, Any],
    renderer_dir: Path,
    staged_dist: Path,
    staged_exe: Path,
    backend_port: int,
    runtime_contract: dict[str, Any],
    central_version: str,
) -> dict[str, Any]:
    """Stage the package: copy runtime, write manifests, verify.

    Returns a dict with keys:
      - source_roots, source_excluded_paths, source_files, source_digest
      - app_dir, runtime_path, python_runtime_path
      - backend_entry_relative
      - staged_verification
    """
    source_roots = package_source_roots(tool_dir, entry)
    source_excluded_paths = package_source_excluded_paths(tool_dir, entry)
    source_excluded_path_set = frozenset(source_excluded_paths)
    source_files = collect_file_hashes(
        PROJECT_ROOT,
        source_roots,
        ignored_directory_names=SOURCE_IGNORED_DIRECTORY_NAMES,
        excluded_relative_paths=source_excluded_path_set,
    )
    source_digest = snapshot_digest(source_files)
    copy_electron_runtime(staged_dist, staged_exe)

    app_dir = staged_dist / "resources" / "app"
    app_dir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(renderer_dir, app_dir / "renderer")
    runtime_path = copy_backend_source_bundle(
        tool_id,
        tool_dir,
        entry,
        app_dir,
    )
    python_runtime_path = copy_portable_python_runtime(app_dir)

    backend_entry_relative = _resolve_backend_entry(tool_id, manifest)
    _write_app_manifests(
        app_dir, tool_id, manifest, backend_entry_relative,
        backend_port, runtime_contract, central_version,
    )
    copy_app_templates(app_dir)
    validate_staged_python_runtime(
        app_dir,
        tool_id,
        backend_entry_relative,
    )

    current_source_files = collect_file_hashes(
        PROJECT_ROOT,
        source_roots,
        ignored_directory_names=SOURCE_IGNORED_DIRECTORY_NAMES,
        excluded_relative_paths=source_excluded_path_set,
    )
    if current_source_files != source_files:
        raise RuntimeError(
            "Package inputs changed during the build; retry after edits finish"
        )

    _write_package_metadata(
        app_dir, tool_id, manifest, backend_port, runtime_contract,
        central_version, backend_entry_relative,
        source_roots, source_excluded_paths, source_files, source_digest,
    )
    staged_verification = verify_packaged_app(
        app_dir,
        project_root=PROJECT_ROOT,
    )
    if not staged_verification.get("ok"):
        raise RuntimeError(
            f"Staged package verification failed: "
            f"{staged_verification.get('message', 'unknown error')}"
        )

    return {
        "source_roots": source_roots,
        "source_excluded_paths": source_excluded_paths,
        "source_files": source_files,
        "source_digest": source_digest,
        "app_dir": app_dir,
        "runtime_path": runtime_path,
        "python_runtime_path": python_runtime_path,
        "backend_entry_relative": backend_entry_relative,
        "staged_verification": staged_verification,
    }


__all__ = ["_stage_package"]
