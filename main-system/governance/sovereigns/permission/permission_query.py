"""Permission Sovereign — Permission Query Adjudication (A6/A10/A22)."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome, verified_basis


class PermissionQueryMixin:
    """Permission query and directory/identity verification."""

    app: Any
    _governance_ref: Any

    def _governance(self) -> Any:
        if self._governance_ref is not None:
            return self._governance_ref
        return getattr(self.app, "governance", None)

    async def _adjudicate_permission_query(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A6/A10/A22: permission query — read-only directory surface."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis("A6"))

        actor = request.payload.get("actor")
        capability = request.payload.get("capability")
        target = request.payload.get("target")
        scope = request.payload.get("scope")

        # Delegate to governed execution (DirectoryAuthority/Authentication)
        outcome = governance.evaluate_permission(
            actor=actor or request.requester,
            capability=capability,
            target=target,
            scope=scope,
        )

        return accepted_outcome(
            {
                "query": "permission.query",
                "decision": "allowed" if outcome.allowed else "denied",
                "basis": outcome.basis,
                "source": "directory-authority",
            },
            verified_basis("A6", "A10", "A22"),
        )

    async def _adjudicate_directory_verify(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A7: directory verification — read-only surface."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis("A7"))

        # DirectoryAuthority verification is delegated
        result = governance.directory_verify(request.payload)

        return accepted_outcome(
            {
                "query": "directory.verify",
                "result": result,
            },
            verified_basis("A7"),
        )

    async def _adjudicate_identity_verify(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A39: identity verification — read-only surface."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis("A39"))

        result = governance.identity_verify(request.payload)

        return accepted_outcome(
            {
                "query": "identity.verify",
                "result": result,
            },
            verified_basis("A39"),
        )