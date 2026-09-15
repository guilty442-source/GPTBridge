"""Permission Sovereign — Authorization Routing and Supervision (A436/A10/E4).

Two-key authorization boundary (A319): every permission.authorize
adjudication must obtain a current 星澄 (Xingcheng) permission-review
finding before deciding.  A deny-objection or missing review fails
closed with a refusal outcome — never an accepted outcome wrapping a
denied decision.

The authorize parameters are aligned with ``MainSystemGovernance.authorize``
(capability, action, target, data_scope, target_tool_id, target_version,
resource_path) — NOT the old (actor, capability, target, scope) shape that
caused TypeErrors.
"""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import (
    accepted_outcome,
    refusal_outcome,
    decision_basis,
    verified_basis,
)
from core_system.permission_grant_ledger import record_grant, record_violation
from .auth_supervision_helpers import AuthSupervisionHelpersMixin


class PermissionAuthSupervisionMixin(AuthSupervisionHelpersMixin):
    """Authorization routing, tool-lifecycle gating, and execution compliance supervision."""

    app: Any
    _governance_ref: Any
    _compliance_violations: list[dict[str, Any]]
    _issued_grants: dict[str, dict[str, Any]]

    # ------------------------------------------------------------------
    # Two-key review (A319): obtain a current 星澄 permission-review finding.
    # ------------------------------------------------------------------

    async def _request_xingcheng_permission_review(
        self, payload: dict[str, Any], requester: str
    ) -> SovereignOutcome | None:
        """Request a 星澄 permission-review; return outcome or None on failure.

        The review goes through 星澄's governed single-gate ``handle`` so
        the second-key review itself carries A446 receipts, requester
        verification and audit publication — never a raw mixin call.
        The sovereign's own identity is proven by a single-use delegation
        session (``_delegation_nonce``), since a bare sovereign-identity
        string is unverifiable and rejected.

        Returns the review outcome (with ``finding`` in result) on success,
        or None if 星澄 is unavailable.  The caller inspects the finding:
        ``pass`` → proceed; ``deny-objection`` → refuse; ``require-change``
        → refuse with a change-required reason.
        """
        xingcheng = getattr(self.app, "xingcheng_sovereign", None)
        if xingcheng is None:
            return None
        child_id = str(getattr(xingcheng, "sovereign_id", "") or "星澄")
        from .._delegation import mint_delegation

        nonce = mint_delegation(
            "permission-sovereign", child_id, "review.permission"
        )
        review_request = SovereignRequest(
            intent="review.permission",
            subject="permission-authorize",
            requester="permission-sovereign",
            payload={**payload, "_delegation_nonce": nonce},
        )
        try:
            return await xingcheng.handle(review_request)
        except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
            return None


    # ------------------------------------------------------------------
    # Master-entry: tool lifecycle gating (called by toolbox_process.py)
    # ------------------------------------------------------------------

    def authorize_tool_lifecycle(self, tool_id: str, action: str) -> None:
        """Master-entry for a tool-lifecycle permission decision."""
        decision_basis("permission-sovereign")
        governance = self._governance()
        if governance is None or not hasattr(governance, "authorize_tool_lifecycle"):
            from governance_rule.permission_directory.execution.path_guard import permission_denied
            raise permission_denied()
        governance.authorize_tool_lifecycle(tool_id, action)

    def can_start_tool(self, tool_id: str) -> bool:
        """Master-entry: can a tool start (permission capability gate)."""
        decision_basis("permission-sovereign")
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
            tool_id, action, target_version, resource_path,
        )

    def create_tool_governance_bootstrap(self, tool_id: str) -> str:
        """Master-entry: mint a governed tool's launch credential."""
        decision_basis("permission-sovereign")
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
        """Master-entry: submit a shared-layer execution request."""
        decision_basis("permission-sovereign")
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
        """Master-entry: cancel a shared-layer execution request."""
        decision_basis("permission-sovereign")
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
        """Master-entry: consume a shared-layer execution response."""
        decision_basis("permission-sovereign")
        governance = self._governance()
        if governance is None or not hasattr(governance, "tool_execution_response"):
            from governance_rule.permission_directory.execution.path_guard import permission_denied
            raise permission_denied()
        return governance.tool_execution_response(tool_id, request_id)

    # ------------------------------------------------------------------
    # Adjudication: permission.authorize (A10/E4) — with two-key review
    # ------------------------------------------------------------------

    async def _adjudicate_permission_authorize(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A10/E4: authorization routing — two-key review then directory decision."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis(("A10", "E4")))

        params = self._extract_authorize_params(request)
        if params is None:
            return refusal_outcome(
                "INSUFFICIENT_AUTHORIZATION_PARAMS", verified_basis(("A10", "E4"))
            )
        # FORBID:self-grant — the permission sovereign may not authorize
        # itself; permission matters touching the sovereign's own identity
        # are denied before any review is requested.
        if str(params["actor"]).strip() == self.sovereign_id:
            return refusal_outcome(
                "SELF_GRANT_DENIED", verified_basis(("A10", "E4"))
            )

        # A319: two-key review gate — fail closed on deny/missing.
        review_refusal = await self._check_two_key_review(request)
        if review_refusal is not None:
            return review_refusal

        return await self._execute_authorization(governance, request, params)

    def _extract_authorize_params(
        self, request: SovereignRequest
    ) -> dict[str, Any] | None:
        """Extract and validate authorize parameters from the request payload."""
        capability = request.payload.get("capability")
        target = request.payload.get("target")
        if not capability or not target:
            return None
        return {
            "capability": capability,
            "action": request.payload.get("action", "execute"),
            "target": target,
            "data_scope": request.payload.get("data_scope")
            or request.payload.get("scope") or "none",
            "target_tool_id": request.payload.get("target_tool_id"),
            "target_version": request.payload.get("target_version"),
            "resource_path": request.payload.get("resource_path"),
            "actor": request.payload.get("actor") or request.requester,
            # permission_id deliberately NOT taken from the payload —
            # E4 PERM-ID:sovereign-managed; a module must not self-issue.
        }

    async def _execute_authorization(
        self, governance: Any, request: SovereignRequest, params: dict[str, Any]
    ) -> SovereignOutcome:
        """Delegate to the governed executor and classify the outcome.

        Includes parallel capability and resource_path validation.
        """
        # Parallel authorization checks: capability match + resource_path validation
        capability = params["capability"]
        resource_path = params.get("resource_path")

        # Check if request has verified claims (from token auth)
        verified_claims = request.payload.get("_verified_claims")
        if verified_claims:
            # Parallel check of capability and resource_path
            capability_valid, resource_valid = await self._check_capability_and_resource_parallel(
                verified_claims, capability, resource_path
            )
            if not capability_valid:
                return refusal_outcome(
                    "CAPABILITY_MISMATCH", verified_basis(("A10", "E4"))
                )
            if not resource_valid:
                return refusal_outcome(
                    "RESOURCE_PATH_VIOLATION", verified_basis(("A10", "E4"))
                )

        try:
            result = governance.authorize(
                capability=params["capability"],
                action=params["action"],
                target=params["target"],
                data_scope=params["data_scope"],
                target_tool_id=params["target_tool_id"],
                target_version=params["target_version"],
                resource_path=params["resource_path"],
            )
        except PermissionError:
            return refusal_outcome(
                "AUTHORIZATION_DENIED", verified_basis(("A10", "E4"))
            )
        if isinstance(result, dict):
            allowed = bool(result.get("allowed"))
        else:
            allowed = result is not None and bool(
                getattr(result, "allowed", True)
            )
        if not allowed:
            return refusal_outcome(
                "AUTHORIZATION_DENIED", verified_basis(("A10", "E4"))
            )
        return self._record_authorized_grant(request, params)


    # ------------------------------------------------------------------
    # Adjudication: permission.supervise (A436)
    # ------------------------------------------------------------------

    async def _adjudicate_permission_supervise(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A436: execution compliance supervision — read-only surface."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis(("A436",)))

        violations = request.payload.get("violations", [])
        if violations:
            try:
                for violation in violations:
                    record_violation(
                        sovereign_id=self.sovereign_id,
                        violation=violation,
                        requester=request.requester,
                        basis=("A436", "A121", "A46"),
                    )
            except (OSError, ValueError, RuntimeError):
                # An unrecorded violation may not be accepted (A121/A46).
                return refusal_outcome(
                    "VIOLATION_LEDGER_UNAVAILABLE",
                    verified_basis(("A436", "A121")),
                )
            self._compliance_violations.extend(violations)

        return accepted_outcome(
            {
                "action": "permission.supervise",
                "violations_recorded": len(violations),
                "total_violations": len(self._compliance_violations),
            },
            verified_basis(("A436",)),
        )

    def get_compliance_violations(self) -> list[dict[str, Any]]:
        """Read-only surface for compliance violations."""
        return list(self._compliance_violations)
