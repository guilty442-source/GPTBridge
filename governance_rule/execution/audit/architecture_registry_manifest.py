"""Manifest-id discovery and registry coverage (split from architecture_registry)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .architecture_registry import components


def discover_manifest_ids(project_root: Path) -> set[str]:
    """Manifest ids from ``*/manifest.json`` plus ``Standalone tools`` at
    depth 2–3.  O2: one ``iterdir`` on the root covers both the depth-1
    glob and locating the tools dir; the tools subtree keeps targeted
    globs (a full ``os.walk`` measurably scans deep trees like
    node_modules — slower, not faster)."""
    root = Path(project_root)
    ids: set[str] = set()
    paths: list[Path] = []
    tools_root: Path | None = None
    try:
        children = sorted(root.iterdir())
    except OSError:
        children = []
    for child in children:
        if child.name.startswith("."):
            continue
        manifest = child / "manifest.json"
        if manifest.is_file():
            paths.append(manifest)
        if child.name == "Standalone tools" and child.is_dir():
            tools_root = child
    if tools_root is not None:
        paths.extend(sorted(tools_root.glob("*/manifest.json")))
        paths.extend(sorted(tools_root.glob("*/*/manifest.json")))
    for path in paths:
        if path.parts and any(part.startswith(".") for part in path.relative_to(root).parts):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tool_id = str(payload.get("id") or "").strip()
        if tool_id:
            ids.add(tool_id)
    return ids


def validate_manifest_coverage(payload: dict[str, Any], project_root: Path) -> list[str]:
    """Every manifest id must be a registered component (and vice versa)."""
    errors: list[str] = []
    registered = {component.component_id for component in components(payload)}
    discovered = discover_manifest_ids(project_root)
    for tool_id in sorted(discovered - registered):
        errors.append(f"manifest is not registered in the architecture registry: {tool_id}")
    for component in components(payload):
        if component.architectural_role != "execution":
            continue
        if (Path(project_root) / component.physical_path / "manifest.json").is_file():
            if component.component_id not in discovered:
                errors.append(
                    f"registered tool has no discoverable manifest: {component.component_id}"
                )
    return errors
