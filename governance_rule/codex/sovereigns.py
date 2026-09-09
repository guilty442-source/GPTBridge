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
        rank="isolated-domain-owner",
        duties=(
            "full-lifecycle-orchestration",
            "dependency-state-integration",
            "sub-sovereign-delegation",
            "runtime-sub-sovereign-delegation",
            "resource-sub-sovereign-delegation",
            "data-sub-sovereign-delegation",
            "integration-sub-sovereign-delegation",
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
        rank="top-orchestrator",
        duties=(
            "update-management",
            "system-health-monitoring",
            "data-integrity-presentation",
            "automatic-repair-coordination",
            "fault-determination",
            "backup-coordination",
            "self-health-test-management",
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
        id="permission-sovereign",
        name="system-permission-sovereign",
        area="permission",
        rank="top-orchestrator",
        duties=(
            "permission-management",
            "permission-issue",
            "permission-termination",
            "permission-supervision",
            "permission-id-management",
            "identity-group-supervision",
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
        rank="isolated-domain-owner",
        duties=(
            "observation",
            "analysis",
            "reasoning",
            "independent-decision",
            "independent-management",
            "owned-domain-execution",
            "model-mode-convergence",
        ),
        powers=(
            "observe",
            "analyze",
            "reason",
            "decide-own-domain",
            "manage-own-domain",
            "authorize-own-domain",
            "execute-own-domain",
            "write-own-domain",
            "delete-own-domain",
            "configure-own-domain",
        ),
        prohibitions=(
            "enter-system-boundary",
            "observe-system",
            "coordinate-system",
            "decide-system",
            "authorize-system",
            "execute-system",
            "override-system",
            "access-system-state",
            "access-system-information",
            "system-effect",
        ),
        basis="codex",
    ),
)


__all__ = ["CodexSovereign", "SOVEREIGNS"]
