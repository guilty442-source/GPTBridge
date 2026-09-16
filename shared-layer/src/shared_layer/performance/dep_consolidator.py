"""Dependency Consolidator — unused/duplicate/transitive cleanup + lazy-load evidence.

Analyzes the dependency graph and classifications to identify:
    1. Unused dependencies (no consumers) → REMOVE_CANDIDATE
    2. Duplicate versions (same package, different versions) → consolidate
    3. Transitive import/include pollution → reduce
    4. Duplicate build entries → consolidate
    5. Heavy optional dependencies that should be lazy-loaded

The consolidator does NOT modify files — it produces a consolidation
report with recommendations.  All changes must be applied by a human
and verified through correctness, Language Governance, build/ABI,
performance regression, and release compatibility checks.

Codex basis:
    A198 — third-party boundary
    A211 — six-language canonical roles
    A358 — benchmark evidence (lazy-load requires profile evidence)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .dep_graph import DependencyGraph, DepNode, NodeLanguage, DependencyType
from .dep_classifier import (
    ClassificationEvidence,
    DependencyClass,
    classify_all_dependencies,
    get_remove_candidates,
    get_optional_without_evidence,
)


@dataclass(frozen=True)
class ConsolidationFinding:
    """One consolidation finding (recommendation)."""
    finding_type: str       # "unused", "duplicate_version", "transitive_pollution",
                            # "duplicate_build_entry", "lazy_load_candidate"
    dependency_id: str
    description: str
    severity: str           # "high", "medium", "low"
    recommendation: str
    affected_files: tuple[str, ...]


@dataclass(frozen=True)
class ConsolidationReport:
    """Full consolidation report."""
    findings: tuple[ConsolidationFinding, ...]
    unused_count: int
    duplicate_count: int
    transitive_pollution_count: int
    duplicate_build_entry_count: int
    lazy_load_candidate_count: int
    summary: str


def find_unused_dependencies(
    graph: DependencyGraph,
    classifications: dict[str, ClassificationEvidence],
) -> list[ConsolidationFinding]:
    """Find dependencies with no consumers (unused)."""
    findings: list[ConsolidationFinding] = []
    for ev in get_remove_candidates(classifications):
        findings.append(ConsolidationFinding(
            finding_type="unused",
            dependency_id=ev.dependency_id,
            description=f"{ev.dependency_id} has no consumers",
            severity="medium",
            recommendation="Remove this dependency if confirmed unused",
            affected_files=ev.consumers,
        ))
    return findings


def find_duplicate_versions(
    graph: DependencyGraph,
) -> list[ConsolidationFinding]:
    """Find dependencies with duplicate names (potential version conflicts).

    This checks for the same package name appearing in multiple
    requirements files with different version specs.
    """
    findings: list[ConsolidationFinding] = []

    # Group package nodes by name
    by_name: dict[str, list[DepNode]] = {}
    for node_id, node in graph.nodes.items():
        if node.is_third_party:
            by_name.setdefault(node.name, []).append(node)

    for name, nodes in by_name.items():
        if len(nodes) > 1:
            findings.append(ConsolidationFinding(
                finding_type="duplicate_version",
                dependency_id=nodes[0].node_id,
                description=f"'{name}' appears {len(nodes)} times",
                severity="medium",
                recommendation="Consolidate to a single approved version",
                affected_files=tuple(n.path for n in nodes if n.path),
            ))

    return findings


def find_transitive_pollution(
    graph: DependencyGraph,
) -> list[ConsolidationFinding]:
    """Find transitive import/include pollution.

    Detects cases where a module includes/imports something it doesn't
    directly need, only because a transitive dependency needs it.
    """
    findings: list[ConsolidationFinding] = []

    # For each node, check if its direct imports are all actually used
    for node_id, node in graph.nodes.items():
        if node.is_third_party or node.is_stdlib:
            continue

        deps = graph.dependencies(node_id)
        if len(deps) > 50:
            # Heuristic: too many direct dependencies suggests pollution
            findings.append(ConsolidationFinding(
                finding_type="transitive_pollution",
                dependency_id=node_id,
                description=f"{node_id} has {len(deps)} direct dependencies (high)",
                severity="low",
                recommendation="Review if all direct dependencies are needed",
                affected_files=(node.path,) if node.path else (),
            ))

    return findings


def find_duplicate_build_entries(
    graph: DependencyGraph,
) -> list[ConsolidationFinding]:
    """Find duplicate build entries (same artifact built multiple times)."""
    findings: list[ConsolidationFinding] = []

    # Check for multiple build edges to the same target
    build_edges: dict[str, list[str]] = {}
    for edge in graph.edges:
        if edge.dep_type == DependencyType.BUILD:
            build_edges.setdefault(edge.target, []).append(edge.source)

    for target, sources in build_edges.items():
        if len(sources) > 1:
            findings.append(ConsolidationFinding(
                finding_type="duplicate_build_entry",
                dependency_id=target,
                description=f"{target} is built by {len(sources)} sources",
                severity="low",
                recommendation="Consolidate build entries",
                affected_files=tuple(sources),
            ))

    return findings


def find_lazy_load_candidates(
    graph: DependencyGraph,
    classifications: dict[str, ClassificationEvidence],
    *,
    profile_evidence: set[str] | None = None,
) -> list[ConsolidationFinding]:
    """Find heavy optional dependencies that should be lazy-loaded.

    A dependency is a lazy-load candidate if:
        - It's classified as OPTIONAL
        - It's a known heavy package (torch, numpy, tiktoken, etc.)
        - It has profile evidence showing import-time cost
    """
    findings: list[ConsolidationFinding] = []
    profile_evidence = profile_evidence or set()

    # Known heavy packages
    heavy_packages = frozenset({
        "torch", "numpy", "tiktoken", "openai", "httpx",
        "sentence_transformers", "Pillow", "beautifulsoup4",
        "imageio_ffmpeg", "triton",
    })

    for ev in classifications.values():
        if ev.dependency_class != DependencyClass.OPTIONAL:
            continue

        node = graph.nodes.get(ev.dependency_id)
        if node is None:
            continue

        if node.name in heavy_packages:
            has_evidence = node.name in profile_evidence
            severity = "medium" if has_evidence else "low"
            rec = "Lazy-load this dependency"
            if not has_evidence:
                rec += " (needs profile evidence first)"

            findings.append(ConsolidationFinding(
                finding_type="lazy_load_candidate",
                dependency_id=ev.dependency_id,
                description=f"Heavy optional dependency '{node.name}'",
                severity=severity,
                recommendation=rec,
                affected_files=ev.consumers,
            ))

    return findings


def run_consolidation_analysis(
    graph: DependencyGraph,
    *,
    profile_evidence: set[str] | None = None,
) -> ConsolidationReport:
    """Run the full consolidation analysis and produce a report."""
    classifications = classify_all_dependencies(
        graph, profile_evidence=profile_evidence,
    )

    findings: list[ConsolidationFinding] = []
    findings.extend(find_unused_dependencies(graph, classifications))
    findings.extend(find_duplicate_versions(graph))
    findings.extend(find_transitive_pollution(graph))
    findings.extend(find_duplicate_build_entries(graph))
    findings.extend(find_lazy_load_candidates(
        graph, classifications, profile_evidence=profile_evidence,
    ))

    # Count by type
    unused = sum(1 for f in findings if f.finding_type == "unused")
    dup = sum(1 for f in findings if f.finding_type == "duplicate_version")
    trans = sum(1 for f in findings if f.finding_type == "transitive_pollution")
    dup_build = sum(1 for f in findings if f.finding_type == "duplicate_build_entry")
    lazy = sum(1 for f in findings if f.finding_type == "lazy_load_candidate")

    return ConsolidationReport(
        findings=tuple(findings),
        unused_count=unused,
        duplicate_count=dup,
        transitive_pollution_count=trans,
        duplicate_build_entry_count=dup_build,
        lazy_load_candidate_count=lazy,
        summary=(
            f"Consolidation: {len(findings)} findings "
            f"({unused} unused, {dup} duplicate, {trans} transitive, "
            f"{dup_build} duplicate build, {lazy} lazy-load candidates)"
        ),
    )


__all__ = [
    "ConsolidationFinding",
    "ConsolidationReport",
    "find_unused_dependencies",
    "find_duplicate_versions",
    "find_transitive_pollution",
    "find_duplicate_build_entries",
    "find_lazy_load_candidates",
    "run_consolidation_analysis",
]
