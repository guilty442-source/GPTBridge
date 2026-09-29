"""Environment doctor — independent tools.

Provides the external and independent tool checks for the environment
doctor.  Node/Electron runtime repair was retired with the Node
toolchain; the C# launcher owns the UI runtime lifecycle.

Windows background subprocess no-window flag: CREATE_NO_WINDOW.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .environment_doctor_constants import (
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

    tool_directories = _discover_tool_directories(project_root)

    for tool_dir in tool_directories:
        _check_tool(
            project_root,
            tool_dir,
            tools,
            invalid_manifests,
            missing_entries,
            missing_executables,
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


def _discover_tool_directories(project_root: Path) -> list[Path]:
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
    return tool_directories


def _check_tool(
    project_root: Path,
    tool_dir: Path,
    tools: list[dict[str, Any]],
    invalid_manifests: list[dict[str, str]],
    missing_entries: list[dict[str, str]],
    missing_executables: list[str],
) -> None:
    if not tool_dir.is_dir() or tool_dir.name.startswith("_"):
        return
    manifest_path = tool_dir / "manifest.json"
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        invalid_manifests.append({"tool": tool_dir.name, "error": str(exc)})
        return
    if not isinstance(manifest, dict):
        invalid_manifests.append({"tool": tool_dir.name, "error": "manifest is not an object"})
        return

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


__all__ = [
    "check_external_tools",
    "check_independent_tools",
]
