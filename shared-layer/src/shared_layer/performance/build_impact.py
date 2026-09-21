"""Build Impact Analyzer — single-file change → minimal rebuild + ABI invalidation.

Given a modified file, determines:
    1. Which artifacts need to be rebuilt (minimal rebuild set)
    2. Whether a public ABI/contract change occurred (invalidates downstream)
    3. Which downstream consumers are affected

The analyzer uses the dependency graph to compute the transitive
closure of dependents.  Only files in the affected set need rebuilding.

ABI/contract changes (public headers, package.json, schema migrations)
trigger broader invalidation than implementation-only changes.

Codex basis:
    A204/A220 — C ABI (ABI changes invalidate downstream)
    A205 — API boundary (API changes invalidate consumers)
    A221 — canonical-native-physical-tree (native ABI boundary)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .dep_graph import DependencyGraph, DepNode, NodeLanguage, DependencyType


class ChangeType(str, Enum):
    IMPLEMENTATION = "implementation"  # internal change, no ABI/API impact
    PUBLIC_ABI = "public_abi"           # public C/C++ header changed
    PUBLIC_API = "public_api"           # public Python/TS API changed
    SCHEMA = "schema"                   # SQL schema/migration changed
    BUILD_CONFIG = "build_config"       # build config changed (package.json, etc.)
    DEPENDENCY = "dependency"           # dependency version changed
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class BuildImpactResult:
    """Result of analyzing a file change's build impact."""
    changed_file: str
    change_type: ChangeType
    directly_affected: tuple[str, ...]     # direct dependents
    transitively_affected: tuple[str, ...] # transitive dependents
    rebuild_set: tuple[str, ...]           # files that need rebuilding
    abi_invalidation: bool                 # whether ABI/API is invalidated
    downstream_consumers: tuple[str, ...]   # consumers that must be re-verified
    summary: str


def classify_change(
    file_path: str,
    diff: str | None = None,
) -> ChangeType:
    """Classify the type of change based on file path and optional diff.

    Heuristics:
        - Public C header (gptbridge_native.h, *.hpp in include/) → PUBLIC_ABI
        - Python __init__.py with export changes → PUBLIC_API
        - package.json / requirements.txt → BUILD_CONFIG or DEPENDENCY
        - SQL migration → SCHEMA
        - Other source files → IMPLEMENTATION
    """
    p = Path(file_path)
    name = p.name
    suffix = p.suffix

    # Public C header
    if name == "gptbridge_native.h" or "/include/" in file_path.replace("\\", "/"):
        return ChangeType.PUBLIC_ABI

    # Private C++ headers (internal, but still ABI-relevant for consumers)
    if suffix in {".hpp", ".h"} and "native" in file_path:
        return ChangeType.PUBLIC_ABI

    # Build config
    if name in {"package.json", "requirements.txt", "pyproject.toml", "tsconfig.json"}:
        return ChangeType.BUILD_CONFIG

    # SQL migration
    if suffix == ".sql" and "migration" in file_path.lower():
        return ChangeType.SCHEMA

    # Python __init__.py — could be public API
    if name == "__init__.py":
        return ChangeType.PUBLIC_API

    # Binding file — ABI boundary
    if name == "_binding.cpp" or name.endswith("_binding.cpp"):
        return ChangeType.PUBLIC_ABI

    # Everything else is implementation
    return ChangeType.IMPLEMENTATION


def analyze_build_impact(
    changed_file: str,
    graph: DependencyGraph,
    *,
    change_type: ChangeType | None = None,
) -> BuildImpactResult:
    """Analyze the build impact of a single file change.

    Uses the dependency graph to find all affected files and determine
    whether ABI/API invalidation is needed.
    """
    return _analyze_build_impact(changed_file, graph, None, change_type)


def _analyze_build_impact(
    changed_file: str,
    graph: DependencyGraph,
    index: dict[str, Any] | None,
    change_type: ChangeType | None = None,
) -> BuildImpactResult:
    if change_type is None:
        change_type = classify_change(changed_file)

    # Find the node for this file
    # Try different ID formats
    node_id = _find_node_id(changed_file, graph, index)

    if node_id is None:
        return BuildImpactResult(
            changed_file=changed_file,
            change_type=change_type,
            directly_affected=(),
            transitively_affected=(),
            rebuild_set=(changed_file,),
            abi_invalidation=change_type in (ChangeType.PUBLIC_ABI, ChangeType.PUBLIC_API),
            downstream_consumers=(),
            summary=f"file not in graph; change_type={change_type.value}",
        )

    # Direct dependents
    direct = tuple(graph.dependents(node_id))

    # Transitive dependents
    transitive = tuple(sorted(graph.transitive_dependents(node_id)))

    # Determine rebuild set and ABI invalidation
    abi_invalidation = change_type in (
        ChangeType.PUBLIC_ABI,
        ChangeType.PUBLIC_API,
        ChangeType.SCHEMA,
        ChangeType.DEPENDENCY,
    )

    if change_type == ChangeType.IMPLEMENTATION:
        # Only rebuild this file + direct dependents that are in the same language
        rebuild_set = (changed_file,) + direct
    elif change_type in (ChangeType.PUBLIC_ABI, ChangeType.PUBLIC_API):
        # Rebuild all transitive dependents
        rebuild_set = (changed_file,) + transitive
    elif change_type == ChangeType.SCHEMA:
        # Schema change: rebuild all SQL consumers
        rebuild_set = (changed_file,) + transitive
    elif change_type == ChangeType.BUILD_CONFIG:
        # Build config change: full rebuild
        rebuild_set = (changed_file,) + transitive
    else:
        rebuild_set = (changed_file,) + transitive

    # Downstream consumers that need re-verification
    if abi_invalidation:
        downstream = transitive
    else:
        downstream = direct

    # Build summary
    parts = [
        f"change_type={change_type.value}",
        f"directly_affected={len(direct)}",
        f"transitively_affected={len(transitive)}",
        f"rebuild_set={len(rebuild_set)}",
        f"abi_invalidation={abi_invalidation}",
    ]
    if abi_invalidation:
        parts.append("downstream consumers must be re-verified")

    return BuildImpactResult(
        changed_file=changed_file,
        change_type=change_type,
        directly_affected=direct,
        transitively_affected=transitive,
        rebuild_set=rebuild_set,
        abi_invalidation=abi_invalidation,
        downstream_consumers=downstream,
        summary="; ".join(parts),
    )


def _build_node_index(graph: DependencyGraph) -> dict[str, Any]:
    """Y5: one pass over ``graph.nodes`` builds reverse indexes.

    ``exact`` maps normalized node paths to the first-matching node id;
    ``by_basename`` maps each node path's final segment to the ordered
    list of node ids (ordered candidate list preserves the original
    first-match-wins semantics of the linear scans); ``by_name`` maps
    ``node.name`` to the first-matching node id.
    """
    exact: dict[str, str] = {}
    by_basename: dict[str, list[str]] = {}
    by_name: dict[str, str] = {}
    for nid, node in graph.nodes.items():
        if node.path:
            normalized = node.path.replace("\\", "/")
            exact.setdefault(normalized, nid)
            by_basename.setdefault(normalized.rsplit("/", 1)[-1], []).append(nid)
        if node.name:
            by_name.setdefault(node.name, nid)
    return {"exact": exact, "by_basename": by_basename, "by_name": by_name}


def _find_node_id(
    file_path: str,
    graph: DependencyGraph,
    index: dict[str, Any] | None = None,
) -> str | None:
    """Find the node ID for a file path in the graph."""
    normalized = file_path.replace("\\", "/")
    if index is None:
        index = _build_node_index(graph)

    # Exact-or-suffix path match: only nodes whose basename matches can
    # satisfy either condition — scan that small candidate list in graph
    # insertion order (same first-match-wins semantics as the original
    # single linear scan).
    basename = normalized.rsplit("/", 1)[-1]
    for nid in index["by_basename"].get(basename, ()):  # ordered by insertion
        node_path = graph.nodes[nid].path
        if not node_path:
            continue
        node_norm = node_path.replace("\\", "/")
        if node_norm == normalized or node_norm.endswith(normalized):
            return nid

    # Try by name
    name = Path(file_path).name
    for candidate in (name, normalized):
        hit = index["by_name"].get(candidate)
        if hit is not None:
            return hit

    # Try common ID formats
    candidates = [
        f"py:{normalized}",
        f"cc:{normalized}",
        f"cc:{Path(file_path).name}",
        f"sql:mig:{Path(file_path).name}",
    ]
    for c in candidates:
        if c in graph.nodes:
            return c

    return None


def get_minimal_rebuild_set(
    changed_files: list[str],
    graph: DependencyGraph,
) -> tuple[str, ...]:
    """Get the minimal rebuild set for multiple file changes.

    Combines the rebuild sets of all changed files, deduplicating.
    """
    all_rebuild: set[str] = set()
    index = _build_node_index(graph)  # one scan shared across all files
    for f in changed_files:
        result = _analyze_build_impact(f, graph, index)
        all_rebuild.update(result.rebuild_set)
    return tuple(sorted(all_rebuild))


__all__ = [
    "ChangeType",
    "BuildImpactResult",
    "classify_change",
    "analyze_build_impact",
    "get_minimal_rebuild_set",
]
