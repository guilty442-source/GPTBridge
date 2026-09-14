"""Permission Sovereign — Permission Lifecycle (terminate, renew, restrict, suspend, revoke).

The sovereign is decision-only (A297): it never executes lifecycle
mutations directly.  Each lifecycle adjudication:

1. Verifies the permission_id was previously issued (via the append-only
   ledger) — fail-closed if no issuance record exists.
2. Appends a new lifecycle entry to the ledger (never mutates the prior
   record).
3. Returns a decision outcome; execution is delegated to the governed
   executor.

The previous in-memory ``_issued_grants`` dict is replaced by the
persistent append-only ledger so grants survive restarts and carry
version/revocation evidence.
"""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome, verified_basis
from core_system.permission_grant_ledger import (
    current_status,
    record_lifecycle,
    was_issued,
)


class PermissionLifecycleMixin:
    """Permission termination, renewal, restriction, suspension, revocation."""

    app: Any
    _governance_ref: Any
    _issued_grants: dict[str, dict[str, Any]]

    async def _adjudicate_permission_terminate(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Permission termination — decision-only, appends to ledger."""
        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", verified_basis(("A436",)))
        if not was_issued(permission_id):
            return refusal_outcome(
                "PERMISSION_NOT_ISSUED", verified_basis(("A436", "A10"))
            )
        status = current_status(permission_id)
        if status and status.get("status") == "terminated":
            return refusal_outcome(
                "PERMISSION_ALREADY_TERMINATED", verified_basis(("A436",))
            )
        record_lifecycle(
            operation="terminate",
            permission_id=permission_id,
            requester=request.requester,
            basis=("A436", "A10", "A22"),
        )
        self._issued_grants[permission_id] = {
            "status": "terminated",
            "terminated_at": self._iso_now(),
            "requester": request.requester,
        }
        return accepted_outcome(
            {
                "action": "permission.terminate",
                "permission_id": permission_id,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis(("A436", "A10", "A22")),
        )

    async def _adjudicate_permission_renew(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Permission renewal — decision-only, appends to ledger."""
        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", verified_basis(("A436",)))
        if not was_issued(permission_id):
            return refusal_outcome(
                "PERMISSION_NOT_ISSUED", verified_basis(("A436", "A10"))
            )
        record_lifecycle(
            operation="renew",
            permission_id=permission_id,
            requester=request.requester,
            basis=("A436", "A10", "A22"),
        )
        self._issued_grants[permission_id] = {
            "status": "renewed",
            "renewed_at": self._iso_now(),
            "requester": request.requester,
        }
        return accepted_outcome(
            {
                "action": "permission.renew",
                "permission_id": permission_id,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis(("A436", "A10", "A22")),
        )

    async def _adjudicate_permission_restrict(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Permission restriction — decision-only, appends to ledger."""
        permission_id = request.payload.get("permission_id")
        restrictions = request.payload.get("restrictions", {})
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", verified_basis(("A436",)))
        if not was_issued(permission_id):
            return refusal_outcome(
                "PERMISSION_NOT_ISSUED", verified_basis(("A436", "A10"))
            )
        record_lifecycle(
            operation="restrict",
            permission_id=permission_id,
            requester=request.requester,
            basis=("A436", "A10", "A22"),
            detail={"restrictions": restrictions},
        )
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
                "execution": "delegated-to-governed-executor",
            },
            verified_basis(("A436", "A10", "A22")),
        )

    async def _adjudicate_permission_suspend(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Permission suspension — decision-only, appends to ledger."""
        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", verified_basis(("A436",)))
        if not was_issued(permission_id):
            return refusal_outcome(
                "PERMISSION_NOT_ISSUED", verified_basis(("A436", "A10"))
            )
        record_lifecycle(
            operation="suspend",
            permission_id=permission_id,
            requester=request.requester,
            basis=("A436", "A10", "A22"),
        )
        self._issued_grants[permission_id] = {
            "status": "suspended",
            "suspended_at": self._iso_now(),
            "requester": request.requester,
        }
        return accepted_outcome(
            {
                "action": "permission.suspend",
                "permission_id": permission_id,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis(("A436", "A10", "A22")),
        )

    async def _adjudicate_permission_revoke(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Permission revocation — decision-only, appends to ledger."""
        permission_id = request.payload.get("permission_id")
        if not permission_id:
            return refusal_outcome("MISSING_PERMISSION_ID", verified_basis(("A436",)))
        if not was_issued(permission_id):
            return refusal_outcome(
                "PERMISSION_NOT_ISSUED", verified_basis(("A436", "A10"))
            )
        record_lifecycle(
            operation="revoke",
            permission_id=permission_id,
            requester=request.requester,
            basis=("A436", "A10", "A22"),
        )
        self._issued_grants[permission_id] = {
            "status": "revoked",
            "revoked_at": self._iso_now(),
            "requester": request.requester,
        }
        return accepted_outcome(
            {
                "action": "permission.revoke",
                "permission_id": permission_id,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis(("A436", "A10", "A22")),
        )
