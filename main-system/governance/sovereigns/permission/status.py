"""Permission Sovereign — Status Surfaces."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase
from core_system.codex_decision import decision_basis
from core_system.versioning import version_registry_status
from governance_rule.permission_directory.code_rule_directory import (
    code_rule_directory_snapshot,
)
from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.governance_policy import (
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


class PermissionStatusMixin:
    """Status reporting surfaces."""

    _issued_grants: dict[str, dict[str, Any]]
    _compliance_violations: list[dict[str, Any]]
    _automation: Any
    _governance_ref: Any
    _started: bool
    sovereign_id: str
    area: str

    def status(self) -> dict[str, Any]:
        return self._with_status_schema({
            "issued_grants": len(self._issued_grants),
            "compliance_violations": len(self._compliance_violations),
            "automation": self._get_automation_status(),
        })

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["grants"] = dict(self._issued_grants)
        base["violations"] = list(self._compliance_violations)
        return base

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "state": "decision-only",
            "owner": self.sovereign_id,
            "issued_grants": len(self._issued_grants),
            "compliance_violations": len(self._compliance_violations),
        }

    def coordination_status(self) -> dict[str, Any]:
        """Snapshot of the permission sovereign (owner of permission matters)."""

        directory = code_rule_directory_snapshot()
        decision = decision_basis(self.area)

        return {
            "role": self.sovereign_id,
            "authority": "permission-management-and-granting",
            "scope": "all-permission-related-matters",
            "management_binding": "codex-bound-per-governance-codex-only",
            "directory_source": "directory-authority-driven",
            "decision_source": decision["decision_source"],
            "codex_version": decision["codex_version"],
            "directory_registry": self.directory_registry_status(),
            "version_registry": self.version_registry_status(),
            "grant": True,
            "terminate": True,
            "permission_ids": "all-modules-managed-by-permission-sovereign",
            "identity_groups": {
                "supervision": "every-module-must-own-dedicated-identity-group",
                "violation_policy": "denied",
                "registered": [
                    {
                        "actor": identity.actor,
                        "tool_id": identity.bound_tool_id,
                        "group_id": identity.group_id,
                        "identity_code": identity.identity_code,
                        "language_name": identity.language_name,
                        "codename": identity.codename,
                    }
                    for identity in identity_group_snapshot().identities
                ],
            },
            "execution": False,
            "supervision": "supervises-execution-compliance",
            "self_grant": False,
            "direct_execution": False,
            "delegation_of_self_execution": False,
            "inheritance": False,
            "privilege_expansion": False,
            "termination_basis": "directive-data-scope-and-governance-codex",
            "grant_scope": "actor-capability-action-target-and-data-scope",
            "approved": {
                "actors": list(directory.approved_actor_names),
                "capabilities": list(directory.approved_capability_names),
                "actions": list(directory.approved_action_names),
                "targets": list(directory.approved_target_names),
                "data_scopes": list(directory.approved_data_scope_names),
            },
            "enforcement": "managed-execution-programs-enforce-but-have-no-authority",
            "decision": decision,
            "master_entry": {
                "delegable": self.can_authorize(),
                "delegation": "governed-executor-only",
            },
        }

    def directory_registry_status(self) -> dict[str, Any]:
        """Unified directory registry snapshot governed by the permission sovereign.

        ALL directory classes are converged under the permission sovereign's
        management.  This surfaces every sealed directory and registry as a
        single coordinated view:

          * code_rule_directory  — approved tool ids, actors, capabilities,
            actions, targets, data scopes, path roots
          * directory_authority  — authority/code version policies, key
            management, access policies, repair boundaries
          * identity_groups      — registered capability identities per module
          * identity_permissions — identity-to-permission bindings
          * capability_boundaries — capability and repair boundaries
          * governance_policy    — sealed governance policy collection
          * version_registry     — code version policy + current version
        """

        code_rules = code_rule_directory_snapshot()
        authority = directory_authority_snapshot()
        identities = identity_group_snapshot()
        permissions = identity_permission_snapshot()
        capabilities, repair_boundaries = capability_boundary_snapshot()
        policy = governance_policy_snapshot()
        version = version_registry_status()

        return {
            "authority": "permission-sovereign",
            "managed_directories": [
                "code-rule-directory",
                "directory-authority",
                "identity-groups",
                "identity-permissions",
                "capability-boundaries",
                "governance-policy",
                "version-registry",
            ],
            "code_rule_directory": {
                "managing_authority": code_rules.managing_authority,
                "approved_tool_ids": list(code_rules.approved_tool_ids),
                "approved_actor_names": list(code_rules.approved_actor_names),
                "approved_capability_names": list(code_rules.approved_capability_names),
                "approved_action_names": list(code_rules.approved_action_names),
                "approved_target_names": list(code_rules.approved_target_names),
                "approved_data_scope_names": list(code_rules.approved_data_scope_names),
                "initial_code_version": code_rules.initial_code_version,
            },
            "directory_authority": {
                "authority_version_policy": {
                    "current_version": authority.authority_version_policy.current_version,
                    "initial_version": authority.authority_version_policy.initial_version,
                    "version_source": "codex",
                },
                "code_version_policy": {
                    "initial_version": authority.code_version_policy.initial_version,
                    "version_source": authority.code_version_policy.version_source,
                    "scope": authority.code_version_policy.scope,
                },
            },
            "identity_groups": {
                "registered_count": len(identities.identities),
                "groups": [
                    {
                        "actor": ident.actor,
                        "tool_id": ident.bound_tool_id,
                        "group_id": ident.group_id,
                        "identity_code": ident.identity_code,
                    }
                    for ident in identities.identities
                ],
            },
            "identity_permissions": {
                "binding_count": len(permissions),
            },
            "capability_boundaries": {
                "capability_count": len(capabilities),
                "repair_boundary_count": len(repair_boundaries),
            },
            "governance_policy": {
                "authority_version": policy.authority_version,
                "managing_authority": policy.authority,
            },
            "version_registry": version,
        }

    def version_registry_status(self) -> dict[str, Any]:
        """Version directory snapshot governed by the permission sovereign.

        The version registry is converged under the permission sovereign's
        management: the ``CodeVersionPolicy`` from the permission directory
        is the authority basis, and the current version is resolved from
        ``main-system/package.json`` under that policy.
        """

        return version_registry_status()

    def can_authorize(self) -> bool:
        governance = self._governance()
        return bool(governance is not None and hasattr(governance, "authorize"))

    def _get_automation_status(self) -> dict[str, Any]:
        if self._automation is None:
            return {"enabled": False}
        return self._automation.status()
