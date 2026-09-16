"""Machine-readable Architecture Registry.

Single source of truth for the system topology: components, architectural
roles, runtime forms, sovereign ownership, execution identities, physical
paths, lifecycle, canonical/degraded status, dependencies and information
channels.  Docs, Codex, manifests and permission registries must agree with
this registry; the governance audit checks that agreement in one pass
(``audit_architecture``).

Authorised by governance_rule; no second copy exists anywhere else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

REGISTRY_RELATIVE: Final[str] = "architecture_registry.json"
SCHEMA_VERSION: Final[int] = 1

REQUIRED_COMPONENT_FIELDS: Final[tuple[str, ...]] = (
    "component_id",
    "architectural_role",
    "runtime_form",
    "owner_sovereign",
    "owner_sub_sovereign",
    "execution_identity",
    "physical_path",
    "lifecycle",
    "canonical",
    "dependencies",
    "information_channels",
)

# Authority kinds that must have exactly one canonical component each.
CANONICAL_AUTHORITY_KINDS: Final[dict[str, str]] = {
    "structured-authority": "postgresql",
    "governance-codex": "codex-authority",
    "semantic-index": "qdrant",
}


class ArchitectureRegistryError(RuntimeError):
    """Raised when the registry payload is structurally invalid."""


def registry_path(audit_dir: Path) -> Path:
    return audit_dir / REGISTRY_RELATIVE


def load_registry(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ArchitectureRegistryError(f"ARCHITECTURE_REGISTRY_UNREADABLE:{error}") from error
    if not isinstance(payload, dict):
        raise ArchitectureRegistryError("ARCHITECTURE_REGISTRY_NOT_AN_OBJECT")
    if int(payload.get("schema_version") or 0) != SCHEMA_VERSION:
        raise ArchitectureRegistryError("ARCHITECTURE_REGISTRY_SCHEMA_VERSION_MISMATCH")
    return payload


@dataclass(frozen=True)
class Component:
    component_id: str
    architectural_role: str
    runtime_form: str
    owner_sovereign: str
    owner_sub_sovereign: str
    execution_identity: str
    physical_path: str
    lifecycle: str
    canonical: bool
    dependencies: tuple[str, ...]
    information_channels: tuple[str, ...]
    external_locator: str = ""


def components(payload: dict[str, Any]) -> list[Component]:
    raw = payload.get("components")
    if not isinstance(raw, list) or not raw:
        raise ArchitectureRegistryError("ARCHITECTURE_REGISTRY_COMPONENTS_MISSING")
    parsed: list[Component] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ArchitectureRegistryError("ARCHITECTURE_REGISTRY_COMPONENT_NOT_AN_OBJECT")
        parsed.append(
            Component(
                component_id=str(entry.get("component_id") or ""),
                architectural_role=str(entry.get("architectural_role") or ""),
                runtime_form=str(entry.get("runtime_form") or ""),
                owner_sovereign=str(entry.get("owner_sovereign") or ""),
                owner_sub_sovereign=str(entry.get("owner_sub_sovereign") or ""),
                execution_identity=str(entry.get("execution_identity") or ""),
                physical_path=str(entry.get("physical_path") or ""),
                lifecycle=str(entry.get("lifecycle") or ""),
                canonical=bool(entry.get("canonical")),
                dependencies=tuple(str(item) for item in entry.get("dependencies") or ()),
                information_channels=tuple(
                    str(item) for item in entry.get("information_channels") or ()
                ),
                external_locator=str(entry.get("external_locator") or ""),
            )
        )
    return parsed


def validate(payload: dict[str, Any], project_root: Path) -> list[str]:
    """Validate schema, taxonomy separation, physical paths and authority rules."""
    errors: list[str] = []
    root = Path(project_root)

    roles = set(payload.get("architectural_roles") or ())
    forms = set(payload.get("runtime_forms") or ())
    sovereigns = payload.get("sovereigns") or {}
    active = set(sovereigns.get("active") or ())
    shims = set(sovereigns.get("compatibility_shims") or ())
    if not roles or not forms:
        errors.append("architecture registry must declare role and runtime_form taxonomies")
    if roles & forms:
        errors.append("architectural_role and runtime_form taxonomies must not overlap")
    if not active:
        errors.append("architecture registry must declare the active sovereign set")

    try:
        parsed = components(payload)
    except ArchitectureRegistryError as error:
        return [str(error)]

    seen: set[str] = set()
    canonical_kinds: dict[str, list[str]] = {}
    for component in parsed:
        if component.component_id in seen:
            errors.append(f"duplicate component id: {component.component_id}")
        seen.add(component.component_id)
        if component.architectural_role not in roles:
            errors.append(f"unknown architectural_role: {component.component_id}:{component.architectural_role}")
        if component.runtime_form not in forms:
            errors.append(f"unknown runtime_form: {component.component_id}:{component.runtime_form}")
        if component.owner_sovereign not in active:
            errors.append(
                f"component owner is not an active sovereign: {component.component_id}:{component.owner_sovereign}"
            )
        if component.owner_sovereign in shims:
            errors.append(
                f"retired sovereign used as owner (compatibility shim only): {component.component_id}"
            )
        if not component.physical_path and not component.external_locator:
            errors.append(f"component lacks physical_path: {component.component_id}")
        elif component.physical_path and not (root / component.physical_path).exists():
            errors.append(
                f"component physical_path does not exist: {component.component_id}:{component.physical_path}"
            )
        if not component.execution_identity:
            errors.append(f"component lacks execution_identity: {component.component_id}")
        if component.canonical:
            kind = str(component.architectural_role)
            canonical_kinds.setdefault(kind, []).append(component.component_id)

    for component in parsed:
        for dependency in component.dependencies:
            if dependency not in seen:
                errors.append(f"unknown dependency: {component.component_id}->{dependency}")

    declared = payload.get("canonical_authorities") or {}
    for kind, expected in CANONICAL_AUTHORITY_KINDS.items():
        actual = declared.get(kind)
        if actual != expected:
            errors.append(f"canonical authority mismatch: {kind}:{actual}")
    canonical_ids = {component.component_id for component in parsed if component.canonical}
    for kind, expected in CANONICAL_AUTHORITY_KINDS.items():
        if expected not in canonical_ids:
            errors.append(f"canonical authority is not marked canonical: {expected}")

    return errors


def validate_flows(payload: dict[str, Any]) -> list[str]:
    """Every declared plane-flow edge must be a registered dependency."""
    errors: list[str] = []
    flow = payload.get("git_plane_flow")
    if not isinstance(flow, list) or not flow:
        return errors
    try:
        by_id = {component.component_id: component for component in components(payload)}
    except ArchitectureRegistryError:
        return errors
    for edge in flow:
        if not isinstance(edge, dict):
            errors.append("git plane flow edge must be an object")
            continue
        source = str(edge.get("from") or "")
        target = str(edge.get("to") or "")
        if source not in by_id or target not in by_id:
            errors.append(f"git plane flow references unknown component: {source}->{target}")
            continue
        if source not in by_id[target].dependencies:
            errors.append(f"git plane flow edge is not a registered dependency: {source}->{target}")
    return errors


def discover_manifest_ids(project_root: Path) -> set[str]:
    root = Path(project_root)
    ids: set[str] = set()
    paths: list[Path] = []
    paths.extend(sorted(root.glob("*/manifest.json")))
    paths.extend(sorted((root / "Standalone tools").glob("*/manifest.json")))
    paths.extend(sorted((root / "Standalone tools").glob("*/*/manifest.json")))
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


__all__ = [
    "CANONICAL_AUTHORITY_KINDS",
    "Component",
    "REGISTRY_RELATIVE",
    "REQUIRED_COMPONENT_FIELDS",
    "SCHEMA_VERSION",
    "ArchitectureRegistryError",
    "components",
    "discover_manifest_ids",
    "load_registry",
    "registry_path",
    "validate",
    "validate_flows",
    "validate_manifest_coverage",
]
