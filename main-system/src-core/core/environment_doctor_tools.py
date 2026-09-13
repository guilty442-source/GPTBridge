"""Environment doctor — independent tools and repair.

Provides the independent tool checks and Electron runtime repair
functions for the environment doctor.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from utils.archive import safe_extract_zip

from .environment_doctor_constants import (
    _background_subprocess_kwargs,
    OPTIONAL_EXTERNAL_TOOLS,
    REQUIRED_EXTERNAL_TOOLS,
)


def _resolve_platform_tool_entry(
    project_root: Path,
    tool_dir: Path,
    manifest: Mapping[str, Any],
) -> Path:
    runtime = manifest.get("runtime")
    if isinstance(runtime, Mapping):
        runtime_entry = str(runtime.get("entry", "")).strip()
        if runtime_entry:
            return (tool_dir / runtime_entry).resolve()

    raw_entry = str(manifest.get("entry", "")).strip()
    if not raw_entry:
        return (tool_dir / "src" / "main.py").resolve()

    entry_path = project_root / raw_entry
    if entry_path.suffix == "":
        entry_path = entry_path.with_suffix(".py")
    return entry_path.resolve()


def check_external_tools() -> dict[str, Any]:
    """Probe required and optional external system-level tools."""
    required_missing: list[str] = []
    required_present: dict[str, bool] = {}
    for label, command in REQUIRED_EXTERNAL_TOOLS.items():
        found = shutil.which(command) is not None
        required_present[label] = found
        if not found:
            required_missing.append(label)
    optional_present: dict[str, bool] = {}
    for label, command in OPTIONAL_EXTERNAL_TOOLS.items():
        optional_present[label] = shutil.which(command) is not None
    return {
        "ok": not required_missing,
        "required": required_present,
        "optional": optional_present,
        "missing": required_missing,
    }


def check_independent_tools(project_root: Path) -> dict[str, Any]:
    tools: list[dict[str, Any]] = []
    invalid_manifests: list[dict[str, str]] = []
    missing_entries: list[dict[str, str]] = []
    missing_executables: list[str] = []

    tool_directories = [
        path
        for path in sorted(project_root.iterdir(), key=lambda item: item.name.casefold())
        if path.is_dir() and (path / "manifest.json").is_file()
    ]
    for host_dir in tuple(tool_directories):
        try:
            host_manifest = json.loads(
                (host_dir / "manifest.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            continue
        declarations = host_manifest.get("companion_tools")
        if not isinstance(declarations, list):
            continue
        for declaration in declarations:
            if not isinstance(declaration, Mapping):
                continue
            relative_path = str(declaration.get("path") or "").strip()
            candidate = (host_dir / relative_path).resolve()
            try:
                candidate.relative_to(host_dir.resolve())
            except ValueError:
                continue
            if (
                candidate.parent == host_dir.resolve()
                and (candidate / "manifest.json").is_file()
            ):
                tool_directories.append(candidate)

    for tool_dir in tool_directories:
        if not tool_dir.is_dir() or tool_dir.name.startswith("_"):
            continue
        manifest_path = tool_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            invalid_manifests.append({"tool": tool_dir.name, "error": str(exc)})
            continue
        if not isinstance(manifest, dict):
            invalid_manifests.append({"tool": tool_dir.name, "error": "manifest is not an object"})
            continue

        tool_id = str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
        entry = _resolve_platform_tool_entry(project_root, tool_dir, manifest)
        executable = manifest.get("executable")
        has_executable = isinstance(executable, Mapping) and bool(
            str(executable.get("path") or executable.get("name") or "").strip()
        )
        distribution = manifest.get("distribution")
        lifecycle = manifest.get("lifecycle")
        explicitly_unpacked = isinstance(distribution, Mapping) and distribution.get("package") is False
        direct_load = isinstance(lifecycle, Mapping) and lifecycle.get("directLoad") is True
        requires_executable = not explicitly_unpacked and not direct_load
        if not entry.exists():
            missing_entries.append({"tool": tool_id, "entry": str(entry)})
        if requires_executable and not has_executable:
            missing_executables.append(tool_id)
        tools.append(
            {
                "id": tool_id,
                "entry": str(entry),
                "entry_exists": entry.exists(),
                "has_executable": has_executable,
                "requires_executable": requires_executable,
            }
        )

    ok = not invalid_manifests and not missing_entries and not missing_executables
    return {
        "ok": ok,
        "count": len(tools),
        "tools": tools,
        "invalid_manifests": invalid_manifests,
        "missing_entries": missing_entries,
        "missing_executables": missing_executables,
    }


def _safe_extract_zip(zip_path: Path, target_dir: Path) -> None:
    safe_extract_zip(zip_path, target_dir)


def _electron_cache_roots() -> list[Path]:
    roots: list[Path] = []
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(Path(local_app_data) / "electron" / "Cache")
    roots.append(Path.home() / ".cache" / "electron")
    return roots


def repair_electron_runtime(project_root: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """Attempt to repair the local Electron runtime."""
    from .environment_doctor_checks import check_electron_runtime

    root = Path(project_root or Path.cwd()).resolve()
    before = check_electron_runtime(root)
    if before["ok"]:
        return {"ok": True, "changed": False, "method": "already_ready", "electron": before}

    electron_root = root / "node_modules" / "electron"
    package_path = electron_root / "package.json"
    installed_version_path = electron_root / "dist" / "version"
    try:
        package_version = str(json.loads(package_path.read_text(encoding="utf-8"))["version"]).lstrip("v")
        installed_version = installed_version_path.read_text(encoding="utf-8").strip().lstrip("v")
        if before["exe_exists"] and package_version == installed_version:
            (electron_root / "path.txt").write_text("electron.exe", encoding="utf-8", newline="\n")
            after_metadata_repair = check_electron_runtime(root)
            if after_metadata_repair["ok"]:
                return {
                    "ok": True,
                    "changed": True,
                    "method": "restore_electron_path_metadata",
                    "electron": after_metadata_repair,
                }
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        pass

    install_script = root / "node_modules" / "electron" / "install.js"
    if install_script.exists() and shutil.which("node"):
        completed = subprocess.run(
            ["node", str(install_script)],
            cwd=str(root),
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=120,
            **_background_subprocess_kwargs(),
        )
        after_node_install = check_electron_runtime(root)
        if after_node_install["ok"]:
            return {
                "ok": True,
                "changed": True,
                "method": "electron_install_script",
                "output": completed.stdout,
                "electron": after_node_install,
            }

    cache_zips: list[Path] = []
    for cache_root in _electron_cache_roots():
        if cache_root.exists():
            cache_zips.extend(cache_root.rglob("electron-v*-win32-x64.zip"))
    cache_zips.sort(key=lambda item: item.stat().st_mtime, reverse=True)

    dist_dir = electron_root / "dist"
    for zip_path in cache_zips:
        try:
            _safe_extract_zip(zip_path, dist_dir)
            (electron_root / "path.txt").write_text("electron.exe", encoding="utf-8", newline="\n")
        except (OSError, RuntimeError, zipfile.BadZipFile):
            continue
        after_cache = check_electron_runtime(root)
        if after_cache["ok"]:
            return {
                "ok": True,
                "changed": True,
                "method": "electron_cache_zip",
                "cache_zip": str(zip_path),
                "electron": after_cache,
            }

    return {
        "ok": False,
        "changed": False,
        "method": "unresolved",
        "electron": check_electron_runtime(root),
    }


__all__ = [
    "check_external_tools",
    "check_independent_tools",
    "repair_electron_runtime",
]
