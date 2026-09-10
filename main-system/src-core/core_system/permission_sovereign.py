"""Permission Sovereign ??permission management and granting authority surface
under the System Sovereign.

The permission sovereign is RESPONSIBLE for ALL permission-related matters per
the Governance Codex only: permission management, granting, termination, the
management of every module's permission IDENTIFIERS, and supervision of
execution against those permissions.  It has NO execution power of its own;
the directory remains directory-driven.

  * role                    = permission-sovereign
  * authority               = permission-management-and-granting
  * scope                   = all permission-related matters
  * management_binding      = codex-bound (per the Governance Codex only)
  * grants                  = true (may grant)
  * terminates              = true (may terminate)
  * permission_ids          = all modules managed by this sovereign
  * execution               = false (no execution power)
  * supervision             = supervises execution compliance

This agent surfaces the directory and the Codex permission decision basis to
the sovereign at the decision level.  Any effective execution is delegated to
the governed-executor (DirectoryAuthority / Authentication); the sovereign
never executes in-process.
"""

from __future__ import annotations

from typing import Any

from governance_rule.code_rule_directory import code_rule_directory_snapshot
from governance_rule.codex import GOVERNANCE_CODEX
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)

from .codex_decision import decision_basis

_PERMISSION_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "permission"),
    None,
)
if _PERMISSION_SOVEREIGN is None:
    raise RuntimeError("permission sovereign not found in Governance Codex")

PERMISSION_SOVEREIGN_RESPONSIBILITIES = _PERMISSION_SOVEREIGN.duties


class PermissionSovereign:
    """In-process sovereign for ALL permission-related matters.

    Owns every permission concern per the Governance Codex ONLY (codex-bound):
    permission management, granting, termination, each module's permission
    IDENTIFIERS, and supervision of execution compliance ??while holding no
    execution power itself.  Every decision references the Governance Codex
    (area ``permission``); it does not own its decision source.  It exposes
    approved identifiers, actors, capabilities, actions, targets and
    data-scopes from the sealed directory plus the Codex's permission decision
    basis.
    """

    ROLE = _PERMISSION_SOVEREIGN.id

    def __init__(self, app: Any, *, governance: Any = None) -> None:
        self.app = app
        self._governance_ref: Any = governance

    # ------------------------------------------------------------------
    # Coordination surface
    # ------------------------------------------------------------------

    def coordination_status(self) -> dict[str, Any]:
        """Snapshot of the permission sovereign (owner of permission matters)."""

        directory = code_rule_directory_snapshot()
        decision = decision_basis(_PERMISSION_SOVEREIGN.area)

        return {
            "role": self.ROLE,
            "authority": "permission-management-and-granting",
            "scope": "all-permission-related-matters",
            "management_binding": "codex-bound-per-governance-codex-only",
            "directory_source": "directory-authority-driven",
            "decision_source": decision["decision_source"],
            "codex_version": decision["codex_version"],
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

    def orchestration_status(self) -> dict[str, Any]:
        """Unified subsystem view for the sovereign orchestration report."""

        return {
            "name": "permission",
            "role": self.ROLE,
            "authority": "permission-management-and-granting",
            "scope": "all-permission-related-matters",
            "management_binding": "codex-bound-per-governance-codex-only",
            "state": "governing",
            "grant": True,
            "terminate": True,
            "permission_ids": "all-modules-managed-by-permission-sovereign",
            "execution": False,
            "supervision": True,
            "decision_source": "governance-codex",
            "delegation": "governed-executor-only",
            "decision": decision_basis(_PERMISSION_SOVEREIGN.area),
        }

    # ------------------------------------------------------------------
    # Authorization master-entry (permission gateway)
    # ------------------------------------------------------------------
    #
    # All permission-related authorization passes through this permission
    # sovereign.  The sovereign references the Governance Codex for its
    # decision basis and DELEGATES the actual directory-driven adjudication to
    # the governed executor (the app's MainSystemGovernance); it never executes
    # in-process itself.

    def _governance(self) -> Any:
        if self._governance_ref is not None:
            return self._governance_ref
        return getattr(self.app, "governance", None)

    def can_authorize(self) -> bool:
        governance = self._governance()
        return bool(governance is not None and hasattr(governance, "authorize"))

    def authorize(
        self,
        *,
        capability: str,
        action: str,
        target: str,
        data_scope: str,
        target_tool_id: str | None = None,
        target_version: str | None = None,
        resource_path: str | None = None,
    ) -> Any:
        """Master-entry for a permission authorization decision.

        References the Codex permission decision basis, then delegates the
        directory-driven adjudication to the governed executor.  Raises
        PermissionError if not delegable.
        """

        decision = decision_basis(_PERMISSION_SOVEREIGN.area)
        governance = self._governance()
        if governance is None or not hasattr(governance, "authorize"):
            from governance_rule.permission_directory.execution.path_guard import permission_denied

            raise permission_denied()
        return governance.authorize(
            capability=capability,
            action=action,
            target=target,
            data_scope=data_scope,
            target_tool_id=target_tool_id,
            target_version=target_version,
            resource_path=resource_path,
        )

    def authorize_tool_lifecycle(self, tool_id: str, action: str) -> None:
        """Master-entry for a tool-lifecycle permission decision.

        References the Codex permission decision basis, then delegates the
        directory-driven adjudication to the governed executor.
        """

        decision_basis(_PERMISSION_SOVEREIGN.area)
        governance = self._governance()
        if governance is None or not hasattr(governance, "authorize_tool_lifecycle"):
            from governance_rule.permission_directory.execution.path_guard import permission_denied

            raise permission_denied()
        governance.authorize_tool_lifecycle(tool_id, action)

    def can_start_tool(self, tool_id: str) -> bool:
        """Master-entry: can a tool start (permission capability gate)."""

        decision_basis(_PERMISSION_SOVEREIGN.area)
        governance = self._governance()
        if governance is None or not hasattr(governance, "can_start_tool"):
            return False
        return bool(governance.can_start_tool(tool_id))

    def authorize_hot_update(
        self,
        tool_id: str,
        action: str,
        target_version: str,
        resource_path: str,
    ) -> None:
        """Master-entry: authorize a versioned (hot) update (E6 gate)."""

        decision_basis("hot-update")
        governance = self._governance()
        if governance is None or not hasattr(governance, "authorize_hot_update"):
            from governance_rule.permission_directory.execution.path_guard import permission_denied

            raise permission_denied()
        governance.authorize_hot_update(
            tool_id,
            action,
            target_version,
            resource_path,
        )

    def create_tool_governance_bootstrap(self, tool_id: str) -> str:
        """Master-entry: mint a governed tool's launch credential.

        References the Codex permission decision basis, then delegates the
        directory-driven bootstrap minting to the governed executor.
        """

        decision_basis(_PERMISSION_SOVEREIGN.area)
        governance = self._governance()
        if governance is None or not hasattr(
            governance, "create_tool_governance_bootstrap"
        ):
            from governance_rule.permission_directory.execution.path_guard import permission_denied

            raise permission_denied()
        return governance.create_tool_governance_bootstrap(tool_id)

    def submit_tool_execution_request(
        self,
        tool_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> None:
        """Master-entry: submit a shared-layer execution request.

        References the Codex permission decision basis, then delegates the
        directory-driven adjudication to the governed executor.
        """

        decision_basis(_PERMISSION_SOVEREIGN.area)
        governance = self._governance()
        if governance is None or not hasattr(
            governance, "submit_tool_execution_request"
        ):
            from governance_rule.permission_directory.execution.path_guard import permission_denied

            raise permission_denied()
        governance.submit_tool_execution_request(tool_id, request_id, payload)

    def cancel_tool_execution_request(
        self,
        tool_id: str,
        request_id: str,
    ) -> bool:
        """Master-entry: cancel a shared-layer execution request.

        References the Codex permission decision basis, then delegates the
        directory-driven adjudication to the governed executor.
        """

        decision_basis(_PERMISSION_SOVEREIGN.area)
        governance = self._governance()
        if governance is None or not hasattr(
            governance, "cancel_tool_execution_request"
        ):
            from governance_rule.permission_directory.execution.path_guard import permission_denied

            raise permission_denied()
        return governance.cancel_tool_execution_request(tool_id, request_id)

    def tool_execution_response(
        self,
        tool_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        """Master-entry: consume a shared-layer execution response.

        References the Codex permission decision basis, then delegates the
        directory-driven adjudication to the governed executor.
        """

        decision_basis(_PERMISSION_SOVEREIGN.area)
        governance = self._governance()
        if governance is None or not hasattr(governance, "tool_execution_response"):
            from governance_rule.permission_directory.execution.path_guard import permission_denied

            raise permission_denied()
        return governance.tool_execution_response(tool_id, request_id)

    def master_entry_status(self) -> dict[str, Any]:
        """Descriptive status of the permission authorization master-entry."""

        return {
            "role": self.ROLE,
            "scope": "all-permission-related-matters",
            "entry": "permission-sovereign-authorization-master-entry",
            "delegation": "governed-executor-only",
            "decision_source": "governance-codex",
            "delegable": self.can_authorize(),
            "surface": [
                "authorize",
                "authorize_tool_lifecycle",
                "can_start_tool",
                "authorize_hot_update",
                "create_tool_governance_bootstrap",
                "submit_tool_execution_request",
                "cancel_tool_execution_request",
                "tool_execution_response",
            ],
            "decision": decision_basis(_PERMISSION_SOVEREIGN.area),
        }


__all__ = ["PERMISSION_SOVEREIGN_RESPONSIBILITIES", "PermissionSovereign"]
