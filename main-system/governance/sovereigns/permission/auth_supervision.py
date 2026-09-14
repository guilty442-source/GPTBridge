"""Permission Sovereign — Authorization Routing and Supervision (A6/A10/E4)."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import (
    accepted_outcome,
    refusal_outcome,
    decision_basis,
    verified_basis,
)


class PermissionAuthSupervisionMixin:
    """Authorization routing, tool-lifecycle gating, and execution compliance supervision."""

    app: Any
    _governance_ref: Any
    _compliance_violations: list[dict[str, Any]]
    _issued_grants: dict[str, dict[str, Any]]

    def _governance(self) -> Any:
        if self._governance_ref is not None:
            return self._governance_ref
        return getattr(self.app, "governance", None)

    def _iso_now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    # ------------------------------------------------------------------
    # Master-entry: tool lifecycle gating (called by toolbox_process.py)
    # ------------------------------------------------------------------

    def authorize_tool_lifecycle(self, tool_id: str, action: str) -> None:
        """Master-entry for a tool-lifecycle permission decision.

        References the Codex permission decision basis, then delegates the
        directory-driven adjudication to the governed executor.
        """

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
        """Master-entry: submit a shared-layer execution request.

        References the Codex permission decision basis, then delegates the
        directory-driven adjudication to the governed executor.
        """

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
        """Master-entry: cancel a shared-layer execution request.

        References the Codex permission decision basis, then delegates the
        directory-driven adjudication to the governed executor.
        """

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
        """Master-entry: consume a shared-layer execution response.

        References the Codex permission decision basis, then delegates the
        directory-driven adjudication to the governed executor.
        """

        decision_basis("permission-sovereign")
        governance = self._governance()
        if governance is None or not hasattr(governance, "tool_execution_response"):
            from governance_rule.permission_directory.execution.path_guard import permission_denied

            raise permission_denied()
        return governance.tool_execution_response(tool_id, request_id)

    # ------------------------------------------------------------------
    # Adjudication: permission.authorize (A10/E4)
    # ------------------------------------------------------------------

    async def _adjudicate_permission_authorize(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A10/E4: authorization routing — decision-only, delegates to DirectoryAuthority."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis("A10", "E4"))

        actor = request.payload.get("actor")
        capability = request.payload.get("capability")
        target = request.payload.get("target")
        scope = request.payload.get("scope")

        if not all([actor, capability, target]):
            return refusal_outcome("INSUFFICIENT_AUTHORIZATION_PARAMS", verified_basis("A10", "E4"))

        result = governance.authorize(
            actor=actor,
            capability=capability,
            target=target,
            scope=scope,
        )

        return accepted_outcome(
            {
                "action": "permission.authorize",
                "decision": "allowed" if result.allowed else "denied",
                "basis": result.basis,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis("A10", "E4"),
        )

    # ------------------------------------------------------------------
    # Adjudication: permission.supervise (A6)
    # ------------------------------------------------------------------

    async def _adjudicate_permission_supervise(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A6: execution compliance supervision — read-only surface."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis("A6"))

        # Surface compliance violations
        violations = request.payload.get("violations", [])
        if violations:
            self._compliance_violations.extend(violations)

        return accepted_outcome(
            {
                "action": "permission.supervise",
                "violations_recorded": len(violations),
                "total_violations": len(self._compliance_violations),
            },
            verified_basis("A6"),
        )

    def get_compliance_violations(self) -> list[dict[str, Any]]:
        """Read-only surface for compliance violations."""
        return list(self._compliance_violations)