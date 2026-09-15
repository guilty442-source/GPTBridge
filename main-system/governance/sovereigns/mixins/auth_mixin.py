"""Sovereign Auth Mixin — requester verification and token authentication."""

from __future__ import annotations

from typing import Any

from .._requester_verification import (
    _GOVERNED_IN_PROCESS_ACTORS,
    verify_requester as _verify_requester_impl,
)


class AuthBase:
    """Mixin providing requester verification and token authentication."""




    async def _verify_requester(self, request: Any) -> bool:
        """验证请求者身份（A10/A11/A116/A121/A435 fail-closed）。

        Delegates to ``_requester_verification.verify_requester`` so the
        fail-closed identity-attestation contract lives in one place.
        """
        return _verify_requester_impl(self, request)

    def _authenticate_token_claims(
        self, request: Any, token: str
    ) -> Any | None:
        """Verify a capability token against this sovereign's request scope."""
        auth = getattr(self.app, "governance_auth", None) or getattr(
            self.app, "governance", None
        )
        if auth is None:
            return None
        auth_service = getattr(auth, "authentication", None) or getattr(
            auth, "authentication_service", None
        )
        if auth_service is None:
            return None
        try:
            claims = auth_service.authenticate_token(token)
        except (ValueError, KeyError, PermissionError, RuntimeError, ImportError):
            # Expected authentication failures deny (fail-closed); unexpected
            # programming errors must surface instead of being downgraded.
            return None
        # The token must prove the requester identity — ``bound_tool_id``
        # belongs to the *requester's* attestation, never to the target
        # sovereign.
        if claims.actor != request.requester:
            return None
        if claims.capability not in {
            "sovereign.request",
            f"{self.area}.request",
            request.intent,
        }:
            return None
        return claims

    def _claims_sovereign_identity(self, requester: str) -> bool:
        """True when the requester string names a sovereign identity."""
        value = str(requester or "").strip()
        if not value:
            return False
        if value.endswith(("-sovereign", "-sub-sovereign")):
            return True
        from ...registries import parent_of, resolve_sovereign

        if parent_of(value) is not None:
            return True
        return resolve_sovereign(self.app, value) is not None


__all__ = ["AuthBase"]