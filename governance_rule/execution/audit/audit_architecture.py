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
    "main-system/governance/sub-sovereigns/__init__.py",
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


__all__ = ["check_architecture_registry"]
