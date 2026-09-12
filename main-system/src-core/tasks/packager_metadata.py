from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from packager_base import (
    PLATFORM_TOOLS_DIR,
    PROJECT_ROOT,
    TOOL_VERSION_PATTERN,
    STANDALONE_BACKEND_PORT_COUNT,
    STANDALONE_BACKEND_PORT_MIN,
    tool_display_version,
)


def standalone_backend_port(tool_id: str) -> int:
    normalized = str(tool_id or "").strip().lower()
    if not normalized:
        raise ValueError("tool_id is required to assign a standalone backend port")
    digest = hashlib.sha256(normalized.encode("utf-8")).digest()
    offset = int.from_bytes(digest[:4], "big") % STANDALONE_BACKEND_PORT_COUNT
    return STANDALONE_BACKEND_PORT_MIN + offset


def load_manifest(tool_dir: Path) -> dict[str, Any] | None:
    manifest_path = tool_dir / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def validate_tool_version_baseline(
    tool_id: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    version = str(manifest.get("version") or "").strip()
    display_version = str(manifest.get("display_version") or "").strip()
    errors: list[str] = []
    if TOOL_VERSION_PATTERN.fullmatch(version) is None:
        errors.append(f"tool version is invalid: {version or 'missing'}")
    expected_display = tool_display_version(version)
    if display_version and display_version != expected_display:
        errors.append(
            f"tool display version must match {expected_display}; found {display_version}"
        )
    return {
        "ok": not errors,
        "tool_id": tool_id,
        "error_code": "TOOL_VERSION_MISMATCH" if errors else "",
        "message": "; ".join(errors),
    }


def resolve_entry(tool_dir: Path, manifest: dict[str, Any]) -> Path:
    runtime = manifest.get("runtime")
    if isinstance(runtime, dict):
        runtime_entry = str(runtime.get("entry", "")).strip()
        if runtime_entry:
            return (tool_dir / runtime_entry).resolve()

    raw_entry = str(manifest.get("entry", "")).strip()
    if not raw_entry:
        return (tool_dir / "src" / "main.py").resolve()

    entry_path = PROJECT_ROOT / raw_entry
    if entry_path.suffix == "":
        entry_path = entry_path.with_suffix(".py")
    # If the entry is relative to the tool directory, resolve from there
    if not (PROJECT_ROOT / raw_entry).exists():
        candidate = tool_dir / raw_entry
        if candidate.exists():
            entry_path = candidate
            if entry_path.suffix == "":
                entry_path = entry_path.with_suffix(".py")
    return entry_path.resolve()


def resolve_executable_name(tool_id: str, manifest: dict[str, Any]) -> str:
    executable = manifest.get("executable")
    if isinstance(executable, dict):
        raw_name = str(executable.get("name", "")).strip()
        if raw_name:
            return Path(raw_name).stem
        raw_path = str(executable.get("path", "")).strip()
        if raw_path:
            return Path(raw_path).stem
    return tool_id


def iter_tools(
    selected_ids: set[str] | None,
    *,
    include_special_unpacked: bool = False,
) -> list[tuple[str, Path, dict[str, Any]]]:
    tools: list[tuple[str, Path, dict[str, Any]]] = []
    if not PLATFORM_TOOLS_DIR.exists():
        return tools

    for tool_dir in sorted(PLATFORM_TOOLS_DIR.iterdir(), key=lambda item: item.name.lower()):
        if not tool_dir.is_dir() or tool_dir.name.startswith("_"):
            continue
        manifest = load_manifest(tool_dir)
        if not manifest:
            continue
        tool_id = str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
        distribution = manifest.get("distribution")
        launch = manifest.get("launch")
        explicitly_selected_hybrid = bool(
            selected_ids is not None
            and tool_id in selected_ids
            and isinstance(launch, dict)
            and str(launch.get("primary") or "").strip().casefold() == "executable"
        )
        if (
            isinstance(distribution, dict)
            and distribution.get("mode") == "special-unpackaged"
            and distribution.get("package") is False
            and not include_special_unpacked
            and not explicitly_selected_hybrid
        ):
            continue
        if selected_ids is not None and tool_id not in selected_ids:
            continue
        tools.append((tool_id, tool_dir, manifest))
    return tools


def iter_companion_renderer_tools(
    selected_ids: set[str] | None,
) -> list[tuple[str, Path, dict[str, Any]]]:
    tools: list[tuple[str, Path, dict[str, Any]]] = []
    if not PLATFORM_TOOLS_DIR.exists():
        return tools
    for host_dir in sorted(
        PLATFORM_TOOLS_DIR.iterdir(), key=lambda item: item.name.lower()
    ):
        if not host_dir.is_dir():
            continue
        host_manifest = load_manifest(host_dir)
        declarations = (
            host_manifest.get("companion_tools")
            if isinstance(host_manifest, dict)
            else None
        )
        if not isinstance(declarations, list):
            continue
        for declaration in declarations:
            if not isinstance(declaration, dict):
                continue
            tool_id = str(declaration.get("id") or "").strip()
            relative = Path(str(declaration.get("path") or "").strip())
            if (
                not tool_id
                or selected_ids is not None
                and tool_id not in selected_ids
                or relative.is_absolute()
                or len(relative.parts) != 1
            ):
                continue
            tool_dir = (host_dir / relative).resolve()
            try:
                tool_dir.relative_to(host_dir.resolve())
            except ValueError:
                continue
            manifest = load_manifest(tool_dir)
            if (
                not isinstance(manifest, dict)
                or manifest.get("id") != tool_id
                or manifest.get("host_tool_id")
                != str(host_manifest.get("id") or host_dir.name)
                or manifest.get("main_system_independent_tool") is not True
                or manifest.get("has_custom_ui") is not True
            ):
                continue
            tools.append((tool_id, tool_dir, manifest))
    return tools


def tool_id_from_directory(tool_dir: Path) -> str:
    manifest = load_manifest(tool_dir)
    if manifest:
        return str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
    return tool_dir.name
