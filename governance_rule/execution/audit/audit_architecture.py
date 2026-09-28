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

import fnmatch
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


_TS_RETIREMENT_TOOL_NOISE = frozenset({
    "venv", "node_modules", "__pycache__",
    "dist", "dist-ui", "build", "release", "releases", "runtime", "out",
})


_PY_ZONE_TOOL_NOISE = frozenset({
    "venv", "node_modules", "__pycache__", "site-packages",
    "dist", "dist-ui", "build", "release", "releases", "out", "target", "temp",
})

_PY_ZONE_REGISTRY = Path(__file__).resolve().parent / "python_zone_registry.json"


def _python_zone(relative: str, rules: list[dict[str, Any]]) -> str:
    """First-match zone classification (registry order is significant)."""
    name = relative.rsplit("/", 1)[-1]
    slashed = "/" + relative
    for rule in rules:
        match = rule.get("match") or {}
        if "prefix" in match and relative.startswith(str(match["prefix"])):
            return str(rule["zone"])
        if "contains" in match and str(match["contains"]) in slashed:
            return str(rule["zone"])
        if "name_glob" in match and fnmatch.fnmatch(name, str(match["name_glob"])):
            return str(rule["zone"])
    return "production"


def check_python_zone_ratchet(root: Path, errors: list[str]) -> None:
    """B4/B73 Python minimization ratchet.

    Every ``.py`` classifies into an allowed zone (governance, training,
    development-verification) or the production fallback.  Production-zone
    files must already exist in the registry ``production_baseline`` — a
    new production Python source fails closed; baseline entries may only
    shrink as migration retires them.  The registry itself is data
    (``python_zone_registry.json``), not Python.
    """
    root = Path(root)
    try:
        registry = json.loads(_PY_ZONE_REGISTRY.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        errors.append(f"python zone registry is unreadable: {error}")
        return
    rules = registry.get("zone_rules")
    baseline = registry.get("production_baseline")
    allowed = registry.get("allowed_zones")
    if not isinstance(rules, list) or not isinstance(baseline, list) \
            or not isinstance(allowed, list):
        errors.append("python zone registry is malformed")
        return
    baseline_set = {str(item) for item in baseline}
    allowed_set = {str(item) for item in allowed}

    new_offenders: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _PY_ZONE_TOOL_NOISE and not d.startswith(".")
        ]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            relative = (Path(dirpath) / name).relative_to(root).as_posix()
            zone = _python_zone(relative, rules)
            if zone in allowed_set:
                continue
            if relative not in baseline_set:
                new_offenders.append(relative)
    if new_offenders:
        shown = ", ".join(sorted(new_offenders)[:20])
        errors.append(
            "new production Python source outside B73 allowed zones "
            f"(not in baseline): {shown}"
            + (f" ... +{len(new_offenders) - 20} more" if len(new_offenders) > 20 else "")
        )


def check_typescript_retirement(root: Path, errors: list[str]) -> None:
    """A348: TypeScript is fully retired in favor of JavaScript-ESM."""
    root = Path(root)
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in _TS_RETIREMENT_TOOL_NOISE and not d.startswith(".")
        ]
        for name in filenames:
            if name.endswith((".ts", ".tsx", ".d.ts")):
                found.append((Path(dirpath) / name).relative_to(root).as_posix())
    if found:
        shown = ", ".join(sorted(found)[:20])
        errors.append(
            "TypeScript retired but authored TypeScript files remain (A348): "
            f"{shown}"
            + (f" ... +{len(found) - 20} more" if len(found) > 20 else "")
        )


__all__ = [
    "check_architecture_registry",
    "check_python_zone_ratchet",
    "check_typescript_retirement",
]
