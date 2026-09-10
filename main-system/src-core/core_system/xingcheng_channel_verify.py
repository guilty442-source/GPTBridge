"""Xingcheng channel verification functions — A189/E164.

Verification of actor access and output separation for the Xingcheng-
exclusive auxiliary information channel.  This module provides **read-only
verification** only — it never decrypts, views, forwards, or stores payload
content.
"""

from __future__ import annotations

from typing import Any

from core_system.xingcheng_channel_types import (
    ENDPOINT_PRINCIPAL,
    NON_ACCESS_ACTORS,
    AccessCheckResult,
)


# ---------------------------------------------------------------------------
# Access verification (A189: PAYLOAD-ACCESS)
# ---------------------------------------------------------------------------

def verify_channel_access(
    actor: str,
    access_type: str,
) -> AccessCheckResult:
    """Verify actor access to the auxiliary channel payload (A189/E164).

    Per A189: ``PAYLOAD-ACCESS:xingcheng-exclusive`` and E164: ``OTHERS:
    view/decrypt/subscribe/search/export/replay/delegate=deny``.

    Only Xingcheng has payload access.  The Permission Sovereign may
    validate channel identity/status/revocation-proof but cannot view
    payload.  All other actors are denied all payload access types.
    """
    if actor == ENDPOINT_PRINCIPAL:
        return AccessCheckResult(
            actor=actor,
            access_type=access_type,
            allowed=True,
            reason="xingcheng-endpoint-principal",
        )

    if actor == "permission-sovereign":
        # A189: may validate identity/status/revocation-proof, but not
        # view/decrypt/search/export/replay/expand payload access.
        if access_type in ("validate-identity", "validate-status", "validate-revocation"):
            return AccessCheckResult(
                actor=actor,
                access_type=access_type,
                allowed=True,
                reason="permission-sovereign-channel-identity-validation-only",
            )
        return AccessCheckResult(
            actor=actor,
            access_type=access_type,
            allowed=False,
            reason="permission-sovereign-payload-access-denied",
        )

    if actor in NON_ACCESS_ACTORS or actor not in (ENDPOINT_PRINCIPAL, "permission-sovereign"):
        return AccessCheckResult(
            actor=actor,
            access_type=access_type,
            allowed=False,
            reason=f"non-xingcheng-payload-access-denied:{actor}",
        )

    return AccessCheckResult(
        actor=actor,
        access_type=access_type,
        allowed=False,
        reason="default-deny",
    )


# ---------------------------------------------------------------------------
# Output separation verification (A189: OUTPUT-SEPARATION)
# ---------------------------------------------------------------------------

def verify_output_separation(
    notification_route: str,
    auxiliary_channel_route: str,
) -> dict[str, Any]:
    """Verify output separation between notification and auxiliary channel (A189).

    Per A189: ``OUTPUT-SEPARATION:any-user-or-system-anomaly-notification is-
    new-redacted-purpose-bound-message over-separate-authorized-information-
    route+never-forward/reveal/private-channel-payload``.
    """
    routes_match = notification_route == auxiliary_channel_route
    return {
        "ok": not routes_match,
        "basis": "A189/E164",
        "notification_route": notification_route,
        "auxiliary_channel_route": auxiliary_channel_route,
        "separation": "separate-authorized-route",
        "forward_payload": False,
        "reveal_payload": False,
        "reason": "" if not routes_match else "routes-must-be-separate",
    }
