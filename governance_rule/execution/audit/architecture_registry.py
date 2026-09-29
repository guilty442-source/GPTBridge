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
    "semantic-index": "vectord",
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

from .architecture_registry_manifest import (
    discover_manifest_ids,
    validate_manifest_coverage,
)
from .architecture_registry_validate import (
    validate,
    validate_dependency_graph,
    validate_flows,
)


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
