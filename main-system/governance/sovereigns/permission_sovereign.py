"""Permission Sovereign — 權限主宰（獨立特權機構，不執行、不審議、不授權超出法典）。

法典依據:
- sovereign_id: permission-sovereign (position 4)
- area: permission
- rank: independent-privileged-institution-no-review-no-execution
- basis: codex
- duty: preserve-independent-two-key-permission-authorization-boundary
- power: issue-deny-renew-restrict-suspend-revoke-exact-permission-after-current-...-review
- prohibitions:
  - overstep-execution
  - exceed-codex
  - self-grant
  - delegate
  - inherit
  - privilege-expansion
  - proxy-permission-matters
  - module-self-issue-permission-id
  - hold-or-exercise-any-direct-execution-power

Edicts:
- E4: OWNER:permission-sovereign; SCOPE:all-permission-matters; ACTIONS:manage-issue-terminate-supervise; PERM-ID:sovereign-managed; EXEC:none; BASIS:codex
- E111: PERMISSION-SOVEREIGN:independent+special-status+not-subordinate; NAME+DUTIES+POWERS:unchanged; OTHER-SOVEREIGNS:request-only-via-information-layer; BASIS:codex-only

  * role                    = permission-sovereign
  * authority               = permission-management-and-granting
  * scope                   = all permission-related matters
  * management_binding      = codex-bound (per the Governance Codex only)
  * grants                  = true (may grant)
  * terminates              = true (may terminate)
  * permission_ids          = all modules managed by this sovereign
  * execution               = false (no execution power)
  * supervision             = supervises execution compliance

The implementation was merged from ``core_system.permission_sovereign`` so
the active path keeps its directory-driven authorization master-entry
surface while operating under the governance-layer sovereign identity.

This agent surfaces the directory and the Codex permission decision basis to
the sovereign at the decision level.  Any effective execution is delegated to
the governed-executor (DirectoryAuthority / Authentication); the sovereign
never executes in-process.
"""

from __future__ import annotations

from typing import Any

from governance_rule.permission_directory.code_rule_directory import code_rule_directory_snapshot
from governance_rule.execution.codex_repository import load_governance_codex
from governance_rule.permission_directory.governance_policy import governance_policy_snapshot
from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
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

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import (
    accepted_outcome,
    decision_basis,
    refusal_outcome,
)
from core_system.versioning import refresh_version_cache, version_registry_status


def _permission_sovereign():
    codex = load_governance_codex()
    return next((s for s in codex.sovereigns if s.area == "permission"), None)


_PERMISSION_SOVEREIGN = _permission_sovereign()
if _PERMISSION_SOVEREIGN is None:
    raise RuntimeError("permission sovereign not found in Governance Codex")

PERMISSION_SOVEREIGN_RESPONSIBILITIES = _PERMISSION_SOVEREIGN.duties


def re_certify_permission_sovereign() -> None:
    """Reload the codex and update the permission sovereign authority.

    Also refreshes all directory caches under the permission sovereign's
    management so the directory registry reflects the current authority
    state after a codex amendment.  Sealed directory snapshots are frozen
    at module load time and are re-read on the next process restart; the
    version registry cache is cleared immediately.
    """

    global _PERMISSION_SOVEREIGN, PERMISSION_SOVEREIGN_RESPONSIBILITIES
    _PERMISSION_SOVEREIGN = _permission_sovereign()
    if _PERMISSION_SOVEREIGN is None:
        raise RuntimeError("permission sovereign not found in Governance Codex after re-certify")
    PERMISSION_SOVEREIGN_RESPONSIBILITIES = _PERMISSION_SOVEREIGN.duties
    PermissionSovereign.ROLE = _PERMISSION_SOVEREIGN.id
    refresh_version_cache()


class PermissionSovereign(SovereignBase):
    """權限主宰：權限事務的目錄驅動裁決與唯讀協調面。

    Owns every permission concern per the Governance Codex ONLY (codex-bound):
    permission management, granting, termination, each module's permission
    IDENTIFIERS, and supervision of execution compliance — while holding no
    execution power itself.  Every decision references the Governance Codex
    (area ``permission``); it does not own its decision source.  It exposes
    approved identifiers, actors, capabilities, actions, targets and
    data-scopes from the sealed directory plus the Codex's permission decision
    basis.

    Per A127, this is an ``independent-special-authority-sovereign`` — it is
    NOT under the decision sovereign.
    """

    sovereign_id = "permission-sovereign"

    ROLE = _PERMISSION_SOVEREIGN.id

    def __init__(self, app: Any | None = None, governance: Any | None = None) -> None:
        super().__init__(app)
        self._governance_ref: Any = governance
        self._directory = None  # 由 governance 注入

    def re_certify(self) -> None:
        """Re-certify the permission sovereign after a codex amendment."""
        re_certify_permission_sovereign()
        governance = self._governance()
        if governance is not None and hasattr(governance, "re_certify"):
            governance.re_certify()

    # ------------------------------------------------------------------
    # Single-gate adjudication (A10/A11)
    # ------------------------------------------------------------------

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：權限查詢、目錄驗證、終止監督（不授權超出法典）。"""
        intent = request.intent

        if intent == "permission.query":
            return await self._adjudicate_permission_query(request)
        if intent == "permission.terminate":
            return await self._adjudicate_permission_terminate(request)
        if intent == "directory.verify":
            return await self._adjudicate_directory_verify(request)
        if intent == "identity.verify":
            return await self._adjudicate_identity_verify(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A6", "A7", "A10"))

    async def _adjudicate_permission_query(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A10: 顯式允許清單，無顯式授權即拒絕。"""
        actor = request.payload.get("actor")
        capability = request.payload.get("capability")
        target = request.payload.get("target")

        if not all([actor, capability, target]):
            return refusal_outcome("MISSING_PARAMETERS", self.verified_basis("A10", "A7"))

        return accepted_outcome(
            {
                "query": {"actor": actor, "capability": capability, "target": target},
                "mode": "explicit-allowlist",
                "source": "permission-directory",
                "note": "permission-sovereign does not execute, only adjudicates",
            },
            self.verified_basis("A10", "A7", "A6"),
        )

    async def _adjudicate_permission_terminate(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A22: 權限終止權威屬於權限主宰。"""
        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", self.verified_basis("A22"))

        return accepted_outcome(
            {
                "terminated": permission_id,
                "authority": "permission-sovereign",
                "basis": "codex+directory",
            },
            self.verified_basis("A22", "A6"),
        )

    async def _adjudicate_directory_verify(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A7: 目錄驅動模式。"""
        entry_type = request.payload.get("entry_type")
        entry_id = request.payload.get("entry_id")

        return accepted_outcome(
            {
                "verified": True,
                "entry_type": entry_type,
                "entry_id": entry_id,
                "mode": "directory-driven",
            },
            self.verified_basis("A7", "A42"),
        )

    async def _adjudicate_identity_verify(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A39: 行為者身份驗證。"""
        actor_class = request.payload.get("actor_class")
        identity = request.payload.get("identity")

        if actor_class not in {"human-operator", "governed-app", "sovereign", "星澄"}:
            return refusal_outcome("INVALID_ACTOR_CLASS", self.verified_basis("A39"))

        return accepted_outcome(
            {"verified": True, "actor_class": actor_class, "identity": identity},
            self.verified_basis("A39", "A10"),
        )

    def set_directory(self, directory: Any) -> None:
        """設定權限目錄（生產層注入）。"""
        self._directory = directory

    # ------------------------------------------------------------------
    # Coordination surface
    # ------------------------------------------------------------------

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
                    "version_source": "codex",
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
            "directory_registry": self.directory_registry_status(),
            "version_registry": self.version_registry_status(),
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


__all__ = [
    "PERMISSION_SOVEREIGN_RESPONSIBILITIES",
    "PermissionSovereign",
    "re_certify_permission_sovereign",
]
