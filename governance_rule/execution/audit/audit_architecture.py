"""Architecture Registry consistency audit.

One audit pass checks that the Codex-authorised Architecture Registry agrees
with reality:

    Codex (governance) == Architecture Registry
      == Permission Directory
      == Module manifests
      == Sovereign ownership
      == Physical directories

Failures here are drift, not warnings: the registry is the machine-readable
topology source of truth.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .architecture_registry import (
    ArchitectureRegistryError,
    components,
    load_registry,
    registry_path,
    validate,
    validate_flows,
    validate_manifest_coverage,
)

_SOVEREIGN_OWNERSHIP_MODULE = (
    "main-system/governance/sovereigns/__init__.py",
)
_PERMISSION_TOOL_ROUTES = (
    "governance_rule/permission_directory/registries/permissions/tool_routes.py"
)


def _registered_tool_ids(payload: dict[str, Any]) -> set[str]:
    return {
        component.component_id
        for component in components(payload)
        if component.architectural_role == "execution"
    }


def _permission_route_ids(project_root: Path) -> set[str]:
    path = Path(project_root) / _PERMISSION_TOOL_ROUTES[0]
    if not path.is_file():
        return set()
    text = path.read_text(encoding="utf-8", errors="replace")
    ids: set[str] = set()
    for marker in ('"tool_id"', "'tool_id'"):
        index = 0
        while True:
            index = text.find(marker, index)
            if index < 0:
                break
            tail = text[index + len(marker):]
            for quote in ('"', "'"):
                start = tail.find(quote)
                if start < 0:
                    continue
                end = tail.find(quote, start + 1)
                if end > 0:
                    candidate = tail[start + 1 : end].strip()
                    if candidate:
                        ids.add(candidate)
                    break
            index += 1
    return ids


def check_architecture_registry(root: Path, errors: list[str]) -> None:
    path = registry_path(Path(__file__).resolve().parent)
    try:
        payload = load_registry(path)
    except ArchitectureRegistryError as error:
        errors.append(f"architecture registry is invalid: {error}")
        return

    errors.extend(validate(payload, root))
    errors.extend(validate_flows(payload))
    errors.extend(validate_manifest_coverage(payload, root))

    registered = _registered_tool_ids(payload)
    routes = _permission_route_ids(root)
    unregistered_routes = routes - registered
    if unregistered_routes:
        errors.append(
            "permission directory tool routes are not in the architecture registry: "
            + ",".join(sorted(unregistered_routes))
        )

    for relative in _SOVEREIGN_OWNERSHIP_MODULE:
        if not (Path(root) / relative).is_file():
            errors.append(f"sovereign ownership module is missing: {relative}")


_TS_RETIREMENT_BASELINE = (
    Path(__file__).resolve().parent / "typescript_grandfathered_baseline.json"
)
_TS_RETIREMENT_TOOL_NOISE = frozenset({
    "venv", "node_modules", "__pycache__",
    "dist", "dist-ui", "build", "release", "releases", "runtime", "out",
})


def check_typescript_retirement(root: Path, errors: list[str]) -> None:
    """A348: TypeScript retired -> JavaScript-ESM.

    Only authored ``.ts``/``.tsx`` sources pinned in the grandfathered
    baseline may exist; any new-authored TypeScript path is denied.
    Migration runs through ``main-system/scripts/ts_to_esm.mjs``.
    """
    try:
        payload = json.loads(
            _TS_RETIREMENT_BASELINE.read_text(encoding="utf-8")
        )
        baseline = {
            str(entry).replace("\\", "/") for entry in payload.get("files", [])
        }
    except (OSError, json.JSONDecodeError) as error:
        errors.append(
            f"typescript grandfathered baseline unreadable: {error}"
        )
        return

    root = Path(root)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _TS_RETIREMENT_TOOL_NOISE and not d.startswith(".")
        ]
        for name in filenames:
            if not name.endswith((".ts", ".tsx")):
                continue
            relative = (Path(dirpath) / name).relative_to(root).as_posix()
            if relative not in baseline:
                found.append(relative)
    if found:
        shown = ", ".join(sorted(found)[:20])
        errors.append(
            "new-authored TypeScript denied (A348 retired -> "
            f"JavaScript-ESM): {shown}"
            + (f" ... +{len(found) - 20} more" if len(found) > 20 else "")
        )


__all__ = ["check_architecture_registry", "check_typescript_retirement"]
