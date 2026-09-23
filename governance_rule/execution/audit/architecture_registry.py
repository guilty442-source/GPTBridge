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

# O1: path-existence cache keyed by parent-directory mtime.  A path's
# existence can only change when its parent directory's mtime changes
# (create/delete/rename), so the signature is conservative — any directory
# mutation invalidates the entry.  Audit-only helper; never persisted.
_PATH_EXISTS_CACHE: dict[str, tuple[int, bool]] = {}


def _path_exists(root: Path, relative: str) -> bool:
    key = str(root / relative)
    try:
        signature = Path(key).parent.stat().st_mtime_ns
    except OSError:
        signature = -1
    cached = _PATH_EXISTS_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    result = Path(key).exists()
    _PATH_EXISTS_CACHE[key] = (signature, result)
    return result

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

# Compatibility shims are retired/renamed sovereign identities kept only for
# one-way resolution; every shim record must declare its replacement and a
# bounded removal condition so a shim can never become a second authority.
REQUIRED_SHIM_FIELDS: Final[tuple[str, ...]] = (
    "sovereign_id",
    "status",
    "replacement",
    "deprecated_since",
    "removal_after",
    "new_reference_forbidden",
)

SHIM_STATUSES: Final[frozenset[str]] = frozenset({"retired", "deprecated"})

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


def compatibility_shim_ids(payload: dict[str, Any]) -> set[str]:
    """Sovereign ids that are compatibility shims only (never owners)."""
    shims: set[str] = set()
    for entry in (payload.get("sovereigns") or {}).get("compatibility_shims") or ():
        if isinstance(entry, dict):
            shim_id = str(entry.get("sovereign_id") or "").strip()
            if shim_id:
                shims.add(shim_id)
        elif isinstance(entry, str) and entry.strip():
            shims.add(entry.strip())
    return shims


def _validate_shims(
    sovereigns: dict[str, Any], active: set[str], errors: list[str]
) -> None:
    """Every shim record declares replacement, deprecation and removal bound."""
    seen: set[str] = set()
    for entry in sovereigns.get("compatibility_shims") or ():
        if not isinstance(entry, dict):
            errors.append("compatibility shim must be a record object")
            continue
        shim_id = str(entry.get("sovereign_id") or "").strip()
        missing = [field for field in REQUIRED_SHIM_FIELDS if field not in entry]
        if missing:
            errors.append(
                f"compatibility shim lacks required fields: {shim_id or '?'}:"
                + ",".join(missing)
            )
            continue
        if not shim_id:
            errors.append("compatibility shim lacks sovereign_id")
            continue
        if shim_id in seen:
            errors.append(f"duplicate compatibility shim: {shim_id}")
        seen.add(shim_id)
        if shim_id in active:
            errors.append(f"compatibility shim is also declared active: {shim_id}")
        status = str(entry.get("status") or "")
        if status not in SHIM_STATUSES:
            errors.append(f"compatibility shim has an invalid status: {shim_id}:{status}")
        replacement = str(entry.get("replacement") or "").strip()
        if not replacement:
            errors.append(f"compatibility shim lacks a replacement: {shim_id}")
        elif replacement == shim_id:
            errors.append(f"compatibility shim replacement must differ: {shim_id}")
        if not str(entry.get("deprecated_since") or "").strip():
            errors.append(f"compatibility shim lacks deprecated_since: {shim_id}")
        if not str(entry.get("removal_after") or "").strip():
            errors.append(f"compatibility shim lacks removal_after: {shim_id}")
        if entry.get("new_reference_forbidden") is not True:
            errors.append(
                f"compatibility shim must forbid new references: {shim_id}"
            )


def validate(payload: dict[str, Any], project_root: Path) -> list[str]:
    """Validate schema, taxonomy separation, physical paths and authority rules."""
    errors: list[str] = []
    root = Path(project_root)

    roles = set(payload.get("architectural_roles") or ())
    forms = set(payload.get("runtime_forms") or ())
    lifecycles = set(payload.get("lifecycles") or ())
    sovereigns = payload.get("sovereigns") or {}
    active = set(sovereigns.get("active") or ())
    shims = compatibility_shim_ids(payload)
    if not roles or not forms:
        errors.append("architecture registry must declare role and runtime_form taxonomies")
    if not lifecycles:
        errors.append("architecture registry must declare the lifecycle taxonomy")
    if roles & forms:
        errors.append("architectural_role and runtime_form taxonomies must not overlap")
    if not active:
        errors.append("architecture registry must declare the active sovereign set")
    _validate_shims(sovereigns, active, errors)

    try:
        parsed = components(payload)
    except ArchitectureRegistryError as error:
        return [str(error)]

    module_labels = payload.get("module_labels")
    trash = module_labels.get("trash") if isinstance(module_labels, dict) else None
    if not isinstance(trash, list):
        if payload.get("registry_id") == "gptbridge-architecture":
            errors.append("architecture registry must declare module_labels.trash")
        trash_ids: set[str] = set()
    else:
        trash_ids = {str(item).strip() for item in trash if str(item).strip()}
        if len(trash_ids) != len(trash):
            errors.append("module_labels.trash contains blank or duplicate identities")

    retired_ids = {
        component.component_id
        for component in parsed
        if component.lifecycle == "retired"
    }
    missing_trash = retired_ids - trash_ids if isinstance(trash, list) else set()
    active_in_trash = trash_ids - retired_ids
    if missing_trash:
        errors.append(
            "retired components missing trash label: " + ",".join(sorted(missing_trash))
        )
    if active_in_trash:
        errors.append(
            "non-retired components carry trash label: " + ",".join(sorted(active_in_trash))
        )

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
        if component.lifecycle not in lifecycles:
            errors.append(f"unknown lifecycle: {component.component_id}:{component.lifecycle}")
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
        elif component.physical_path and not _path_exists(root, component.physical_path):
            errors.append(
                f"component physical_path does not exist: {component.component_id}:{component.physical_path}"
            )
        if not component.execution_identity:
            errors.append(f"component lacks execution_identity: {component.component_id}")
        elif component.execution_identity in shims:
            errors.append(
                f"retired sovereign used as execution_identity (compatibility shim only): {component.component_id}"
            )
        elif component.lifecycle != "retired" and (
            component.execution_identity == "sub-sovereign"
            or component.execution_identity.endswith("-sub-sovereign")
        ):
            errors.append(
                f"retired sub-sovereign used as execution_identity: {component.component_id}"
            )
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

    errors.extend(validate_dependency_graph(payload))

    return errors


def validate_dependency_graph(payload: dict[str, Any]) -> list[str]:
    """The component dependency graph must be a single acyclic DAG.

    Unknown dependencies are reported by :func:`validate`; here only real
    cycles are failures — a cycle means the registry has no unique
    topological authority order.
    """
    errors: list[str] = []
    try:
        parsed = components(payload)
    except ArchitectureRegistryError:
        return errors
    graph = {
        component.component_id: tuple(
            dependency for dependency in component.dependencies if dependency
        )
        for component in parsed
    }
    visiting: list[str] = []
    # X7: position index so cycle extraction is O(1) instead of
    # ``visiting.index()`` O(N) per dependency edge (O(N²) overall).
    visiting_pos: dict[str, int] = {}
    state: dict[str, int] = {node: 0 for node in graph}  # 0=new 1=visiting 2=done

    def visit(node: str) -> bool:
        state[node] = 1
        visiting_pos[node] = len(visiting)
        visiting.append(node)
        for dependency in graph.get(node, ()):
            if dependency not in graph:
                continue
            if state[dependency] == 1:
                cycle = visiting[visiting_pos[dependency]:] + [dependency]
                errors.append(
                    "dependency graph contains a cycle: " + "->".join(cycle)
                )
                return True
            if state[dependency] == 0 and visit(dependency):
                return True
        visiting.pop()
        del visiting_pos[node]
        state[node] = 2
        return False

    for node in graph:
        if state[node] == 0 and visit(node):
            break
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


__all__ = [
    "CANONICAL_AUTHORITY_KINDS",
    "Component",
    "REGISTRY_RELATIVE",
    "REQUIRED_COMPONENT_FIELDS",
    "REQUIRED_SHIM_FIELDS",
    "SCHEMA_VERSION",
    "SHIM_STATUSES",
    "ArchitectureRegistryError",
    "compatibility_shim_ids",
    "components",
    "discover_manifest_ids",
    "load_registry",
    "registry_path",
    "validate",
    "validate_dependency_graph",
    "validate_flows",
    "validate_manifest_coverage",
]
