"""Permission auth supervision helpers mixin (A185 split).

Contains the _check_two_key_review and _record_authorized_grant methods
extracted from PermissionAuthSupervisionMixin.
"""
from __future__ import annotations

from typing import Any

from .._base import SovereignOutcome, SovereignRequest
from core_system.codex_decision import (
    accepted_outcome,
    refusal_outcome,
    verified_basis,
)
from core_system.permission_grant_ledger import record_grant


class AuthSupervisionHelpersMixin:
    """Two-key review and grant recording helpers."""

    _issued_grants: dict[str, dict[str, Any]]



    async def _check_two_key_review(
        self,
        request: SovereignRequest,
        *,
        capability: str | None = None,
        target: str | None = None,
        purpose: str | None = None,
    ) -> SovereignOutcome | None:
        """A319 two-key gate: return a refusal outcome if the 星澄 review
        denies or is missing; return None to proceed when the review passes.

        Per the dual-key boundary, a missing review (星澄 unavailable) fails
        closed — authorization cannot proceed without the second key.
        Review aspects are forwarded as actually supplied: absent evidence
        is marked ``missing``, never fabricated as ``present``.
        """
        review_payload: dict[str, Any] = {
            "actor": request.payload.get("actor") or request.requester,
            "capability": capability or request.payload.get("capability"),
            "target": target or request.payload.get("target"),
            "scope": request.payload.get("data_scope") or request.payload.get("scope"),
            "purpose": purpose or request.payload.get("purpose") or "authorize",
            "basis": request.payload.get("basis") or request.payload.get("codex_ref"),
        }
        for aspect in ("least_privilege", "separation", "expiry", "risk", "evidence"):
            review_payload[aspect] = request.payload.get(aspect, "missing")
        review = await self._request_xingcheng_permission_review(
            review_payload, request.requester
        )
        if review is None:
            return refusal_outcome(
                "TWO_KEY_REVIEW_UNAVAILABLE", verified_basis(("A319", "A10"))
            )
        result = review.result if isinstance(review.result, dict) else {}
        finding = result.get("finding", "")
        if finding == "deny-objection":
            return refusal_outcome(
                "TWO_KEY_REVIEW_DENIED", verified_basis(("A319", "A10"))
            )
        if finding == "require-change":
            return refusal_outcome(
                "TWO_KEY_REVIEW_REQUIRES_CHANGE", verified_basis(("A319",))
            )
        if finding != "pass":
            return refusal_outcome(
                "TWO_KEY_REVIEW_NOT_PASSED", verified_basis(("A319", "A10"))
            )
        return None  # proceed to authorization

    def _record_authorized_grant(
        self, request: SovereignRequest, params: dict[str, Any]
    ) -> SovereignOutcome:
        """Record the issued grant in the ledger and return the accepted outcome.

        E4 PERM-ID:sovereign-managed — the permission id is minted here,
        never taken from the request payload (FORBID:module-self-issue-
        permission-id).
        """
        import secrets

        permission_id = f"perm-{secrets.token_hex(8)}"
        try:
            record_grant(
                permission_id=permission_id,
                actor=params["actor"],
                capability=params["capability"],
                target=params["target"],
                action=params["action"],
                data_scope=params["data_scope"],
                requester=request.requester,
                review_finding="pass",
                basis=("A10", "E4", "A319"),
            )
        except (OSError, ValueError, RuntimeError):
            # An unrecorded grant may not proceed (A46/A121 fail-closed):
            # expected ledger failures deny instead of silently degrading.
            return refusal_outcome(
                "PERMISSION_LEDGER_UNAVAILABLE",
                verified_basis(("A10", "A46", "A121")),
            )
        self._issued_grants[permission_id] = {
            "status": "issued",
            "issued_at": self._iso_now(),
            "requester": request.requester,
        }
        return accepted_outcome(
            {
                "action": "permission.authorize",
                "decision": "allowed",
                "permission_id": permission_id,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis(("A10", "E4", "A319")),
        )


__all__ = ["AuthSupervisionHelpersMixin"]
