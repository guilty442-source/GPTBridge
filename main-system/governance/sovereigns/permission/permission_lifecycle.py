"""Permission Sovereign — Permission Lifecycle (terminate, renew, restrict, suspend, revoke)."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome, verified_basis


class PermissionLifecycleMixin:
    """Permission termination, renewal, restriction, suspension, revocation."""

    app: Any
    _governance_ref: Any
    _issued_grants: dict[str, dict[str, Any]]

    def _governance(self) -> Any:
        if self._governance_ref is not None:
            return self._governance_ref
        return getattr(self.app, "governance", None)

    async def _adjudicate_permission_terminate(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Permission termination — read-only surface, no execution."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis("A6"))

        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", verified_basis("A6"))

        # Delegate to governed execution
        result = governance.terminate_permission(permission_id)

        # Record for read-only surface
        self._issued_grants[permission_id] = {
            "status": "terminated",
            "terminated_at": self._iso_now(),
            "requester": request.requester,
        }

        return accepted_outcome(
            {
                "action": "permission.terminate",
                "permission_id": permission_id,
                "result": result,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis("A6", "A10", "A22"),
        )

    async def _adjudicate_permission_renew(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Permission renewal."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis("A6"))

        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", verified_basis("A6"))

        result = governance.renew_permission(permission_id)

        self._issued_grants[permission_id] = {
            "status": "renewed",
            "renewed_at": self._iso_now(),
            "requester": request.requester,
        }

        return accepted_outcome(
            {
                "action": "permission.renew",
                "permission_id": permission_id,
                "result": result,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis("A6", "A10", "A22"),
        )

    async def _adjudicate_permission_restrict(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Permission restriction."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis("A6"))

        permission_id = request.payload.get("permission_id")
        restrictions = request.payload.get("restrictions", {})
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", verified_basis("A6"))

        result = governance.restrict_permission(permission_id, restrictions)

        self._issued_grants[permission_id] = {
            "status": "restricted",
            "restricted_at": self._iso_now(),
            "restrictions": restrictions,
            "requester": request.requester,
        }

        return accepted_outcome(
            {
                "action": "permission.restrict",
                "permission_id": permission_id,
                "result": result,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis("A6", "A10", "A22"),
        )

    async def _adjudicate_permission_suspend(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Permission suspension."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis("A6"))

        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", verified_basis("A6"))

        result = governance.suspend_permission(permission_id)

        self._issued_grants[permission_id] = {
            "status": "suspended",
            "suspended_at": self._iso_now(),
            "requester": request.requester,
        }

        return accepted_outcome(
            {
                "action": "permission.suspend",
                "permission_id": permission_id,
                "result": result,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis("A6", "A10", "A22"),
        )

    async def _adjudicate_permission_revoke(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Permission revocation."""
        governance = self._governance()
        if governance is None:
            return refusal_outcome("GOVERNANCE_UNAVAILABLE", verified_basis("A6"))

        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", verified_basis("A6"))

        result = governance.revoke_permission(permission_id)

        self._issued_grants[permission_id] = {
            "status": "revoked",
            "revoked_at": self._iso_now(),
            "requester": request.requester,
        }

        return accepted_outcome(
            {
                "action": "permission.revoke",
                "permission_id": permission_id,
                "result": result,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis("A6", "A10", "A22"),
        )

    def _iso_now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()