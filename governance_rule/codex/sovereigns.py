"""Sovereign sub-laws (主宰子法) — authoritative code-form tokens only.

Each sovereign is declared as a frozen dataclass with duties, explicit powers
(where any), and prohibitions.  This module contains no executable functions,
only immutable declarations.  Chinese translations live in
``governance_rule/codex/sovereigns_chinese.py`` and are backup-only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Tuple


@dataclass(frozen=True)
class CodexSovereign:
    """A sovereign's sub-law: scope, duties, powers, and prohibitions."""

    id: str
    name: str
    area: str
    rank: str
    duties: Tuple[str, ...]
    powers: Tuple[str, ...]
    prohibitions: Tuple[str, ...]
    basis: str


SOVEREIGNS: Final[Tuple[CodexSovereign, ...]] = (
    CodexSovereign(
        id="governance-authority",
        name="governance-authority",
        area="governance",
        rank="supreme-rule-layer-maintenance",
        duties=(
            "guard-codex-immutability",
            "guard-codex-integrity",
            "maintain-highest-rule-layer",
        ),
        powers=(),
        prohibitions=(
            "subordinate-proxy-governance",
            "override-governance-codex",
        ),
        basis="codex",
    ),
    CodexSovereign(
        id="system-sovereign",
        name="system-sovereign",
        area="system-sovereign",
        rank="top-orchestrator",
        duties=(
            "full-lifecycle-orchestration",
            "dependency-state-integration",
            "sub-sovereign-delegation",
        ),
        powers=(),
        prohibitions=(
            "overstep-execution",
            "hold-execution-power",
            "decide-sub-sovereign-details",
            "direct-execute-sub-sovereign-work",
            "proxy-permission-matters",
        ),
        basis="codex",
    ),
    CodexSovereign(
        id="maintenance-sovereign",
        name="maintenance-sovereign",
        area="maintenance",
        rank="sub-sovereign",
        duties=(
            "update-management",
            "system-health-monitoring",
            "data-integrity-presentation",
            "automatic-repair-coordination",
            "fault-determination",
            "backup-coordination",
        ),
        powers=(),
        prohibitions=(
            "overstep-execution",
            "exceed-codex",
            "ignore-or-hide-data-integrity-abnormal",
            "proxy-data-integrity-check",
            "proxy-permission-matters",
        ),
        basis="codex",
    ),
    CodexSovereign(
        id="runtime-sovereign",
        name="runtime-sovereign",
        area="runtime",
        rank="sub-sovereign",
        duties=(
            "process-survival",
            "service-maintenance",
            "runtime-integrity",
        ),
        powers=(),
        prohibitions=(
            "overstep-execution",
            "exceed-codex",
        ),
        basis="codex",
    ),
    CodexSovereign(
        id="resource-sovereign",
        name="resource-sovereign",
        area="resource",
        rank="sub-sovereign",
        duties=(
            "memory-state-monitor",
            "disk-state-monitor",
            "model-state-monitor",
            "compute-state-monitor",
            "resource-provision",
            "resource-delegate",
            "resource-release",
        ),
        powers=(),
        prohibitions=(
            "overstep-execution",
            "overstep-data",
            "overstep-permission",
        ),
        basis="codex",
    ),
    CodexSovereign(
        id="data-sovereign",
        name="data-sovereign",
        area="data",
        rank="sub-sovereign",
        duties=(
            "structured-data-access-spec",
            "semantic-index-access-spec",
            "version-history-access-spec",
            "consistency-check",
            "integrity-check",
            "data-directory",
        ),
        powers=(),
        prohibitions=(
            "overstep-execution",
            "overstep-resource",
            "overstep-permission",
            "proxy-health-monitor",
        ),
        basis="codex",
    ),
    CodexSovereign(
        id="integration-sovereign",
        name="integration-sovereign",
        area="integration",
        rank="sub-sovereign",
        duties=(
            "cross-sovereign-structural-interface",
            "cross-module-structural-interface",
            "channel-coordination",
            "sync-mechanism",
            "bus-coordination",
        ),
        powers=(),
        prohibitions=(
            "decision-layer-coordinate",
            "overstep-execution",
            "proxy-xingcheng-decision",
        ),
        basis="codex",
    ),
    CodexSovereign(
        id="permission-sovereign",
        name="permission-sovereign",
        area="permission",
        rank="sub-sovereign",
        duties=(
            "permission-management",
            "permission-issue",
            "permission-termination",
            "permission-supervision",
            "permission-id-management",
        ),
        powers=(),
        prohibitions=(
            "overstep-execution",
            "exceed-codex",
            "self-grant",
            "delegate",
            "inherit",
            "privilege-expansion",
            "proxy-permission-matters",
            "module-self-issue-permission-id",
        ),
        basis="codex",
    ),
    CodexSovereign(
        id="xingcheng",
        name="xingcheng",
        area="xingcheng",
        rank="peer-of-system-sovereign",
        duties=(
            "observation",
            "analysis",
            "reasoning",
            "advice",
            "coordination",
            "explanation",
            "management-thinking",
        ),
        powers=(
            "observe",
            "analyze",
            "reason",
            "advise",
            "coordinate",
            "explain",
        ),
        prohibitions=(
            "direct-execution",
            "grant-authorization",
            "override-decision",
            "override-state",
            "self-reason-override-codex",
            "hold-system-exec",
            "proxy-integration-structural-interface",
        ),
        basis="codex",
    ),
)


__all__ = ["CodexSovereign", "SOVEREIGNS"]
