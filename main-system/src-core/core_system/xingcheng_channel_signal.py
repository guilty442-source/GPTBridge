"""Xingcheng channel signal functions — A189/E164.

Failure signal and channel status for the Xingcheng-exclusive auxiliary
information channel.  This module provides **signal-only** output — it
never decrypts, views, forwards, or stores payload content.
"""

from __future__ import annotations

from typing import Any

from core_system.xingcheng_channel_types import (
    AUDIT_RESTRICTIONS,
    CHANNEL_OWNER,
    ENDPOINT_PRINCIPAL,
    XINGCHENG_AUXILIARY_CHANNEL_ID,
)


# ---------------------------------------------------------------------------
# Failure handling (A189: FAILURE)
# ---------------------------------------------------------------------------

def failure_signal(
    uncertainty_type: str,
) -> dict[str, Any]:
    """Produce a failure signal for channel uncertainty (A189: FAILURE).

    Per A189: ``FAILURE:identity/key/session/integrity/sequence/expiry
    uncertainty=>deny+drop-content+publish-generic-channel-health-only``.
    """
    return {
        "signal_type": "xingcheng-auxiliary-channel-failure",
        "authority": "signal-only",
        "basis": "A189/E164",
        "uncertainty_type": uncertainty_type,
        "action": "deny+drop-content+publish-generic-channel-health-only",
        "content_preserved": False,
        "generic_health_only": True,
    }


# ---------------------------------------------------------------------------
# Channel status (A189: AUDIT — metadata-only)
# ---------------------------------------------------------------------------

def channel_status(
    *,
    message_count: int = 0,
    last_state: str = "idle",
    session_generation: str = "",
) -> dict[str, Any]:
    """Return the auxiliary channel status for audit (A189: AUDIT).

    Per A189: ``AUDIT:metadata-only+no-payload/hash/embedding/preview/token``.
    This status contains only metadata; no payload content is included.
    """
    return {
        "channel_id": XINGCHENG_AUXILIARY_CHANNEL_ID,
        "owner": CHANNEL_OWNER,
        "endpoint_principal": ENDPOINT_PRINCIPAL,
        "message_count": message_count,
        "last_state": last_state,
        "session_generation": session_generation,
        "basis": "A189/E164",
        "audit_scope": "metadata-only",
        "audit_restrictions": list(AUDIT_RESTRICTIONS),
        "payload_included": False,
    }
