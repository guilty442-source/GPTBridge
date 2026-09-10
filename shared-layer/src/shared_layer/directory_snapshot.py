"""directory_snapshot — information-layer access to the permission directory.

Per A153/E50, information-layer channels are exclusive to the information
layer.  This module provides the governed read surface for all directory
classes managed by the Permission Sovereign (A127).

External modules that need directory information should import from here
(the shared-layer information channel) rather than directly from
``governance_rule.permission_directory.*``.  The physical directory storage
lives under ``governance_rule/permission_directory/`` and is managed by the
Permission Sovereign; this module is the information-layer gateway.

Authority: permission-sovereign (A127 independent-special-authority)
Physical storage: governance_rule/permission_directory/
Information channel: shared-layer (A153/E50)
"""

from __future__ import annotations

from typing import Any

from governance_rule.permission_directory.code_rule_directory import (
    CodeRuleDirectorySnapshot,
    code_rule_directory_snapshot,
)
from governance_rule.permission_directory.directory_authority import (
    DirectoryAuthoritySnapshot,
    directory_authority_snapshot,
)
from governance_rule.permission_directory.governance_policy import (
    GovernancePolicy,
    governance_policy_snapshot,
)
from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
    capability_boundary_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)


def directory_snapshot() -> dict[str, Any]:
    """Unified directory snapshot via the information layer.

    Returns a coordinated view of all directory classes managed by the
    Permission Sovereign.  This is the information-layer read surface;
    external consumers should use this instead of direct imports from
    ``governance_rule.permission_directory.*``.
    """

    code_rules = code_rule_directory_snapshot()
    authority = directory_authority_snapshot()
    identities = identity_group_snapshot()
    permissions = identity_permission_snapshot()
    capabilities, repair_boundaries = capability_boundary_snapshot()
    policy = governance_policy_snapshot()

    return {
        "authority": "permission-sovereign",
        "channel": "shared-layer-information",
        "managed_directories": [
            "code-rule-directory",
            "directory-authority",
            "identity-groups",
            "identity-permissions",
            "capability-boundaries",
            "governance-policy",
        ],
        "code_rule_directory": code_rules,
        "directory_authority": authority,
        "identity_groups": identities,
        "identity_permissions": permissions,
        "capability_boundaries": capabilities,
        "repair_boundaries": repair_boundaries,
        "governance_policy": policy,
    }


__all__ = [
    "CodeRuleDirectorySnapshot",
    "DirectoryAuthoritySnapshot",
    "GovernancePolicy",
    "capability_boundary_snapshot",
    "code_rule_directory_snapshot",
    "directory_authority_snapshot",
    "directory_snapshot",
    "governance_policy_snapshot",
    "identity_group_snapshot",
    "identity_permission_snapshot",
]
