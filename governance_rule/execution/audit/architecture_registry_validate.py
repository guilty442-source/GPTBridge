"""Registry validators (split from architecture_registry)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .architecture_registry import (
    CANONICAL_AUTHORITY_KINDS,
    REQUIRED_SHIM_FIELDS,
    SHIM_STATUSES,
    ArchitectureRegistryError,
    _path_exists,
    compatibility_shim_ids,
    components,
)


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



def _validate_component_fields(
    payload: dict[str, Any], root: Path, parsed, roles, forms,
    lifecycles, active, shims, errors: list[str],
) -> set[str]:
    # per-component field checks + dependency-reference checks
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
    return seen


def _validate_canonical(payload, parsed, errors: list[str]) -> None:
    declared = payload.get("canonical_authorities") or {}
    for kind, expected in CANONICAL_AUTHORITY_KINDS.items():
        actual = declared.get(kind)
        if actual != expected:
            errors.append(f"canonical authority mismatch: {kind}:{actual}")
    canonical_ids = {component.component_id for component in parsed if component.canonical}
    for kind, expected in CANONICAL_AUTHORITY_KINDS.items():
        if expected not in canonical_ids:
            errors.append(f"canonical authority is not marked canonical: {expected}")


def _validate_g100(payload, errors: list[str]) -> None:
    # G100 five-core convergence: every non-retired component with a
    # physical path must bind to a codex architecture row
    # (architecture_code) and its block/unit lineage; virtual components
    # (external_locator only) are exempt.
    raw_components = payload.get("components") or []
    for entry in raw_components:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("lifecycle") or "") == "retired":
            continue
        if not str(entry.get("physical_path") or "").strip():
            continue
        cid = str(entry.get("component_id") or "?")
        if not str(entry.get("architecture_code") or "").strip():
            errors.append(
                f"component lacks architecture_code (codex directory binding): {cid}"
            )
        elif "block" not in entry or "unit" not in entry:
            # Keys must exist even when the codex lineage legitimately
            # has no block/unit ancestor (BLOCK_TOOLS members,
            # project-root-bound components).
            errors.append(f"component lacks block/unit lineage fields: {cid}")

    # G100 Python-residency convergence: every non-retired python-process
    # component must carry a declared disposition — governance/bounded
    # retention or a concrete native migration target.
    residency_kinds = set(payload.get("python_residency_kinds") or [])
    for entry in raw_components:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("lifecycle") or "") == "retired":
            continue
        if str(entry.get("runtime_form") or "") != "python-process":
            continue
        cid = str(entry.get("component_id") or "?")
        disposition = str(entry.get("python_residency") or "")
        if not residency_kinds:
            errors.append("python_residency_kinds registry list is missing")
            break
        if disposition not in residency_kinds:
            errors.append(
                f"python-process component lacks a declared residency disposition: {cid}"
            )


def _validate_five_cores(payload, active, seen, errors: list[str]) -> None:
    # The five_cores section must cover exactly the active sovereign set —
    # every core declares its sovereign owner, target unit and live
    # process surface; no active sovereign may lack a core entry.
    cores = payload.get("five_cores")
    if payload.get("registry_id") == "gptbridge-architecture":
        if not isinstance(cores, dict) or not cores:
            errors.append("architecture registry must declare five_cores")
            cores = {}
    if isinstance(cores, dict):
        covered = set()
        for core_id, entry in cores.items():
            if not isinstance(entry, dict):
                errors.append(f"five_cores entry must be an object: {core_id}")
                continue
            owner = str(entry.get("owner_sovereign") or "")
            if owner in active:
                covered.add(owner)
            else:
                errors.append(
                    f"five_cores owner is not an active sovereign: {core_id}:{owner}"
                )
            if not str(entry.get("unit") or "").strip():
                errors.append(f"five_cores entry lacks unit: {core_id}")
            processes = entry.get("processes")
            if not isinstance(processes, list) or not processes:
                errors.append(f"five_cores entry lacks processes: {core_id}")
            else:
                for proc in processes:
                    if str(proc) not in seen:
                        errors.append(
                            f"five_cores process is not a component: {core_id}:{proc}"
                        )
        missing = active - covered
        if missing:
            errors.append(
                "active sovereigns lack five_cores coverage: "
                + ",".join(sorted(missing))
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
    seen = _validate_component_fields(
        payload, root, parsed, roles, forms, lifecycles, active, shims, errors)
    _validate_canonical(payload, parsed, errors)
    _validate_g100(payload, errors)
    _validate_five_cores(payload, active, seen, errors)

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
