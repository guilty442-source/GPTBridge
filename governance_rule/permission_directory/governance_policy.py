"""Re-export shim for governance policy (A185 dedup).

This module previously duplicated ``governance_rule/governance_policy.py``.
It now re-exports the canonical definitions so existing imports continue
to work without maintaining two copies of the policy.
"""
from __future__ import annotations

from governance_rule.governance_policy import (  # noqa: F401
    DEFAULT_ACTIVE_GOVERNANCE_RULES,
    GOVERNANCE_POLICY,
    GOVERNANCE_RULE_CATALOG,
    AutomaticRepairPolicy,
    BoundaryEscapePolicy,
    CodeArchitecturePolicy,
    GovernanceActivationPolicy,
    GovernancePolicy,
    GovernanceWorkflowPolicy,
    IdentifierLabelPolicy,
    IdentityAuthenticationPolicy,
    IndependentToolSeparationPolicy,
    LocalizationPolicy,
    PermissionDistributionPolicy,
    SharedLayerPolicy,
    SystemResponsibilityPolicy,
    governance_policy_snapshot,
)

__all__ = [
    "DEFAULT_ACTIVE_GOVERNANCE_RULES",
    "GOVERNANCE_POLICY",
    "GOVERNANCE_RULE_CATALOG",
    "AutomaticRepairPolicy",
    "BoundaryEscapePolicy",
    "CodeArchitecturePolicy",
    "GovernanceActivationPolicy",
    "GovernancePolicy",
    "GovernanceWorkflowPolicy",
    "IdentifierLabelPolicy",
    "IdentityAuthenticationPolicy",
    "IndependentToolSeparationPolicy",
    "LocalizationPolicy",
    "PermissionDistributionPolicy",
    "SharedLayerPolicy",
    "SystemResponsibilityPolicy",
    "governance_policy_snapshot",
]
