"""Dependency Classifier — classify dependencies by usage scope.

Classifies each dependency into one of:
    RUNTIME_REQUIRED  — needed at runtime, failure blocks startup
    BUILD_ONLY         — needed only during build, not at runtime
    TEST_ONLY          — needed only for tests
    OPTIONAL           — optional, lazy-loaded with profile evidence
    NATIVE_TOOLCHAIN   — compiler/linker toolchain for native build
    REMOVE_CANDIDATE   — unused or redundant, candidate for removal

Classification is evidence-based, using the dependency graph and
heuristic rules.  The classifier does NOT remove dependencies — it
only flags candidates for human review.

Codex basis:
    A198 — third-party boundary (classification respects boundaries)
    A211 — six-language canonical roles
    A358 — benchmark evidence (OPTIONAL requires profile evidence)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .dep_graph import DependencyGraph, DepNode, NodeLanguage, DependencyType


class DependencyClass(str, Enum):
    RUNTIME_REQUIRED = "RUNTIME_REQUIRED"
    BUILD_ONLY = "BUILD_ONLY"
    TEST_ONLY = "TEST_ONLY"
    OPTIONAL = "OPTIONAL"
    NATIVE_TOOLCHAIN = "NATIVE_TOOLCHAIN"
    REMOVE_CANDIDATE = "REMOVE_CANDIDATE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ClassificationEvidence:
    """Evidence supporting a dependency classification."""
    dependency_id: str
    dependency_class: DependencyClass
    reasons: tuple[str, ...]
    consumers: tuple[str, ...]  # who uses this dependency
    has_profile_evidence: bool  # for OPTIONAL: profile evidence exists


# Known runtime-required Python packages
_PYTHON_RUNTIME_REQUIRED = frozenset({
    "psutil", "psycopg", "qdrant_client", "websockets",
})

# Known build-only Python packages
_PYTHON_BUILD_ONLY = frozenset({
    "pybind11", "pyinstaller", "setuptools",
})

# Known test-only Python packages
_PYTHON_TEST_ONLY = frozenset({
    "pytest", "pytest_asyncio", "pytest_xdist",
})

# Known optional Python packages (heavy, lazy-loaded)
_PYTHON_OPTIONAL = frozenset({
    "tiktoken", "openai", "httpx", "numpy", "torch",
    "sentence_transformers", "Pillow", "beautifulsoup4",
    "imageio_ffmpeg", "triton",
})

# Known native toolchain packages
_NATIVE_TOOLCHAIN = frozenset({
    "pybind11",  # Also build-only, but specifically native toolchain
})

# Known TypeScript runtime packages
_TS_RUNTIME_REQUIRED = frozenset({
    "react", "react-dom",
})

# Known TypeScript build-only packages
_TS_BUILD_ONLY = frozenset({
    "electron", "electron-builder", "vite",
    "@vitejs/plugin-react", "typescript",
    "ts-node", "madge",
})

# Known TypeScript type-only packages
_TS_TEST_ONLY = frozenset({
    "@types/node", "@types/react", "@types/react-dom",
})


def classify_dependency(
    node: DepNode,
    graph: DependencyGraph,
    *,
    profile_evidence: set[str] | None = None,
) -> ClassificationEvidence:
    """Classify a single dependency node.

    Uses the dependency graph to determine consumers and applies
    heuristic rules based on known package lists and usage patterns.
    """
    profile_evidence = profile_evidence or set()
    consumers = tuple(graph.dependents(node.node_id))
    reasons: list[str] = []

    if node.language == NodeLanguage.PYTHON:
        return _classify_python(node, consumers, profile_evidence, reasons)
    elif node.language == NodeLanguage.TYPESCRIPT:
        return _classify_typescript(node, consumers, reasons)
    elif node.language in (NodeLanguage.C, NodeLanguage.CPP):
        return _classify_cc(node, consumers, reasons)
    elif node.language == NodeLanguage.SQL:
        return _classify_sql(node, consumers, reasons)
    else:
        return ClassificationEvidence(
            dependency_id=node.node_id,
            dependency_class=DependencyClass.UNKNOWN,
            reasons=("unknown language",),
            consumers=consumers,
            has_profile_evidence=False,
        )


def _classify_python(
    node: DepNode,
    consumers: tuple[str, ...],
    profile_evidence: set[str],
    reasons: list[str],
) -> ClassificationEvidence:
    """Classify a Python dependency."""
    name = node.name

    # Check if it's a package node (py:pkg:xxx)
    if node.node_id.startswith("py:pkg:"):
        if name in _PYTHON_RUNTIME_REQUIRED:
            reasons.append("known runtime-required package")
            return ClassificationEvidence(
                dependency_id=node.node_id,
                dependency_class=DependencyClass.RUNTIME_REQUIRED,
                reasons=tuple(reasons),
                consumers=consumers,
                has_profile_evidence=False,
            )
        if name in _PYTHON_BUILD_ONLY:
            reasons.append("known build-only package")
            return ClassificationEvidence(
                dependency_id=node.node_id,
                dependency_class=DependencyClass.BUILD_ONLY,
                reasons=tuple(reasons),
                consumers=consumers,
                has_profile_evidence=False,
            )
        if name in _PYTHON_TEST_ONLY:
            reasons.append("known test-only package")
            return ClassificationEvidence(
                dependency_id=node.node_id,
                dependency_class=DependencyClass.TEST_ONLY,
                reasons=tuple(reasons),
                consumers=consumers,
                has_profile_evidence=False,
            )
        if name in _PYTHON_OPTIONAL:
            has_evidence = name in profile_evidence
            if has_evidence:
                reasons.append("optional package with profile evidence for lazy load")
            else:
                reasons.append("optional package (no profile evidence yet)")
            return ClassificationEvidence(
                dependency_id=node.node_id,
                dependency_class=DependencyClass.OPTIONAL,
                reasons=tuple(reasons),
                consumers=consumers,
                has_profile_evidence=has_evidence,
            )
        if name in _NATIVE_TOOLCHAIN:
            reasons.append("native toolchain package")
            return ClassificationEvidence(
                dependency_id=node.node_id,
                dependency_class=DependencyClass.NATIVE_TOOLCHAIN,
                reasons=tuple(reasons),
                consumers=consumers,
                has_profile_evidence=False,
            )
        if node.is_stdlib:
            reasons.append("stdlib module")
            return ClassificationEvidence(
                dependency_id=node.node_id,
                dependency_class=DependencyClass.RUNTIME_REQUIRED,
                reasons=tuple(reasons),
                consumers=consumers,
                has_profile_evidence=False,
            )
        # Unknown third-party: check if it has consumers
        if not consumers:
            reasons.append("no consumers found — candidate for removal")
            return ClassificationEvidence(
                dependency_id=node.node_id,
                dependency_class=DependencyClass.REMOVE_CANDIDATE,
                reasons=tuple(reasons),
                consumers=consumers,
                has_profile_evidence=False,
            )
        reasons.append(f"has {len(consumers)} consumer(s)")
        return ClassificationEvidence(
            dependency_id=node.node_id,
            dependency_class=DependencyClass.RUNTIME_REQUIRED,
            reasons=tuple(reasons),
            consumers=consumers,
            has_profile_evidence=False,
        )

    # File node: always runtime (it's source code)
    return ClassificationEvidence(
        dependency_id=node.node_id,
        dependency_class=DependencyClass.RUNTIME_REQUIRED,
        reasons=("source file",),
        consumers=consumers,
        has_profile_evidence=False,
    )


def _classify_typescript(
    node: DepNode,
    consumers: tuple[str, ...],
    reasons: list[str],
) -> ClassificationEvidence:
    """Classify a TypeScript dependency."""
    name = node.name

    if name in _TS_RUNTIME_REQUIRED:
        reasons.append("known runtime-required package")
        return ClassificationEvidence(
            dependency_id=node.node_id,
            dependency_class=DependencyClass.RUNTIME_REQUIRED,
            reasons=tuple(reasons),
            consumers=consumers,
            has_profile_evidence=False,
        )
    if name in _TS_BUILD_ONLY:
        reasons.append("known build-only package")
        return ClassificationEvidence(
            dependency_id=node.node_id,
            dependency_class=DependencyClass.BUILD_ONLY,
            reasons=tuple(reasons),
            consumers=consumers,
            has_profile_evidence=False,
        )
    if name in _TS_TEST_ONLY:
        reasons.append("type definition / test-only package")
        return ClassificationEvidence(
            dependency_id=node.node_id,
            dependency_class=DependencyClass.TEST_ONLY,
            reasons=tuple(reasons),
            consumers=consumers,
            has_profile_evidence=False,
        )
    if not consumers:
        reasons.append("no consumers found — candidate for removal")
        return ClassificationEvidence(
            dependency_id=node.node_id,
            dependency_class=DependencyClass.REMOVE_CANDIDATE,
            reasons=tuple(reasons),
            consumers=consumers,
            has_profile_evidence=False,
        )
    reasons.append(f"has {len(consumers)} consumer(s)")
    return ClassificationEvidence(
        dependency_id=node.node_id,
        dependency_class=DependencyClass.RUNTIME_REQUIRED,
        reasons=tuple(reasons),
        consumers=consumers,
        has_profile_evidence=False,
    )


def _classify_cc(
    node: DepNode,
    consumers: tuple[str, ...],
    reasons: list[str],
) -> ClassificationEvidence:
    """Classify a C/C++ dependency."""
    if node.is_stdlib:
        reasons.append("C/C++ standard library header")
        return ClassificationEvidence(
            dependency_id=node.node_id,
            dependency_class=DependencyClass.RUNTIME_REQUIRED,
            reasons=tuple(reasons),
            consumers=consumers,
            has_profile_evidence=False,
        )
    if not consumers and node.node_id.startswith("cc:inc:"):
        reasons.append("no consumers — unused include candidate")
        return ClassificationEvidence(
            dependency_id=node.node_id,
            dependency_class=DependencyClass.REMOVE_CANDIDATE,
            reasons=tuple(reasons),
            consumers=consumers,
            has_profile_evidence=False,
        )
    # Source files are runtime
    return ClassificationEvidence(
        dependency_id=node.node_id,
        dependency_class=DependencyClass.RUNTIME_REQUIRED,
        reasons=("C/C++ source/header",),
        consumers=consumers,
        has_profile_evidence=False,
    )


def _classify_sql(
    node: DepNode,
    consumers: tuple[str, ...],
    reasons: list[str],
) -> ClassificationEvidence:
    """Classify a SQL dependency."""
    if node.node_id.startswith("sql:mig:"):
        reasons.append("SQL migration")
        return ClassificationEvidence(
            dependency_id=node.node_id,
            dependency_class=DependencyClass.RUNTIME_REQUIRED,
            reasons=tuple(reasons),
            consumers=consumers,
            has_profile_evidence=False,
        )
    if node.node_id.startswith("sql:table:"):
        if not consumers:
            reasons.append("no consumers — orphan table candidate")
            return ClassificationEvidence(
                dependency_id=node.node_id,
                dependency_class=DependencyClass.REMOVE_CANDIDATE,
                reasons=tuple(reasons),
                consumers=consumers,
                has_profile_evidence=False,
            )
        reasons.append("SQL table with consumers")
        return ClassificationEvidence(
            dependency_id=node.node_id,
            dependency_class=DependencyClass.RUNTIME_REQUIRED,
            reasons=tuple(reasons),
            consumers=consumers,
            has_profile_evidence=False,
        )
    return ClassificationEvidence(
        dependency_id=node.node_id,
        dependency_class=DependencyClass.UNKNOWN,
        reasons=("unknown SQL node",),
        consumers=consumers,
        has_profile_evidence=False,
    )


def classify_all_dependencies(
    graph: DependencyGraph,
    *,
    profile_evidence: set[str] | None = None,
) -> dict[str, ClassificationEvidence]:
    """Classify all dependencies in a graph."""
    results: dict[str, ClassificationEvidence] = {}
    for node_id, node in graph.nodes.items():
        results[node_id] = classify_dependency(
            node, graph, profile_evidence=profile_evidence,
        )
    return results


def get_remove_candidates(
    classifications: dict[str, ClassificationEvidence],
) -> list[ClassificationEvidence]:
    """Get all dependencies classified as REMOVE_CANDIDATE."""
    return [
        ev for ev in classifications.values()
        if ev.dependency_class == DependencyClass.REMOVE_CANDIDATE
    ]


def get_optional_without_evidence(
    classifications: dict[str, ClassificationEvidence],
) -> list[ClassificationEvidence]:
    """Get OPTIONAL dependencies without profile evidence."""
    return [
        ev for ev in classifications.values()
        if ev.dependency_class == DependencyClass.OPTIONAL
        and not ev.has_profile_evidence
    ]


__all__ = [
    "DependencyClass",
    "ClassificationEvidence",
    "classify_dependency",
    "classify_all_dependencies",
    "get_remove_candidates",
    "get_optional_without_evidence",
]
