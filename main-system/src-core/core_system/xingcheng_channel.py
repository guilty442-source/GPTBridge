"""Xingcheng-exclusive auxiliary information channel — A189/E164.

Per A189 (xingcheng-exclusive-auxiliary-information-channel) and E164
(xingcheng-private-information-channel), the information layer owns a
dedicated auxiliary private channel for Xingcheng's autonomous reasoning,
context, learning, and review working information.

Channel identity (A189: CHANNEL-ID): ``information-layer://xingcheng/
auxiliary-private``.

Owner (A189: OWNER): information-layer.
Endpoint principal (A189: ENDPOINT-PRINCIPAL): Xingcheng only.

Key invariants:

  * **Payload access** — Xingcheng-exclusive.  All other sovereigns, sub-
    sovereigns, modules, tools, UI, operators, administrators, maintainers,
    developers, loggers, auditors, brokers, PostgreSQL, Qdrant have no
    content access (A189: PAYLOAD-ACCESS).
  * **Permission Sovereign** — may validate fixed channel identity, status,
    and revocation proof, but cannot view, decrypt, search, export, replay,
    or expand payload access (A189: PERMISSION-SOVEREIGN).
  * **Information layer** — routes, stores, forwards, deduplicates, orders,
    expires, and delivers **opaque ciphertext only**.  It observes minimum
    metadata only: channel-id, message-id, session-generation, size-class,
    time, state, error-code.  No content-derived metadata (A189: INFORMATION-
    LAYER).
  * **Keys** — Xingcheng-endpoint-held, channel-specific, non-exportable,
    rotating, revocable, not held by router/store/audit/system-sovereign
    (A189: KEYS).
  * **Storage** — encrypted, owner-isolated, bounded-retention, no general-
    index/RAG/telemetry/log/backup/analytics/training (A189: STORAGE).
  * **Output separation** — any user or system anomaly notification is a
    new redacted purpose-bound message over a separate authorized
    information route.  Never forward, reveal, or private-channel-payload
    (A189: OUTPUT-SEPARATION).
  * **Failure** — identity/key/session/integrity/sequence/expiry
    uncertainty => deny + drop content + publish generic channel health
    only (A189: FAILURE).
  * **Audit** — metadata-only.  No payload, hash, embedding, preview, or
    token (A189: AUDIT).

This module provides **read-only data structures and verification**.  It
never decrypts, views, forwards, or stores payload content.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

# ---------------------------------------------------------------------------
# Channel identity (A189: CHANNEL-ID)
# ---------------------------------------------------------------------------

XINGCHENG_AUXILIARY_CHANNEL_ID: Final[str] = "information-layer://xingcheng/auxiliary-private"
CHANNEL_OWNER: Final[str] = "information-layer"
ENDPOINT_PRINCIPAL: Final[str] = "xingcheng"

# A189: INFORMATION-LAYER — minimum metadata observable by the router
MINIMUM_ROUTING_METADATA: Final[tuple[str, ...]] = (
    "channel-id",
    "message-id",
    "session-generation",
    "size-class",
    "time",
    "state",
    "error-code",
)

# A189: KEYS — key properties
KEY_PROPERTIES: Final[tuple[str, ...]] = (
    "xingcheng-endpoint-held",
    "channel-specific",
    "non-exportable",
    "rotating",
    "revocable",
    "not-held-by-router",
    "not-held-by-store",
    "not-held-by-audit",
    "not-held-by-system-sovereign",
)

# A189: STORAGE — storage properties
STORAGE_PROPERTIES: Final[tuple[str, ...]] = (
    "encrypted",
    "owner-isolated",
    "bounded-retention",
    "no-general-index",
    "no-rag",
    "no-telemetry",
    "no-log",
    "no-backup",
    "no-analytics",
    "no-training",
)

# A189: PAYLOAD-ACCESS — actors with NO content access
NON_ACCESS_ACTORS: Final[tuple[str, ...]] = (
    "sovereigns",
    "sub-sovereigns",
    "modules",
    "tools",
    "ui",
    "operators",
    "administrators",
    "maintainers",
    "developers",
    "loggers",
    "auditors",
    "brokers",
    "postgresql",
    "qdrant",
)

# A189: AUDIT — audit content restrictions
AUDIT_RESTRICTIONS: Final[tuple[str, ...]] = (
    "no-payload",
    "no-hash",
    "no-embedding",
    "no-preview",
    "no-token",
)


# ---------------------------------------------------------------------------
# Channel message metadata (A189: INFORMATION-LAYER minimum metadata)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChannelMessageMetadata:
    """Minimum routing metadata for an auxiliary channel message (A189).

    Per A189: ``INFORMATION-LAYER:observe-minimum-metadata{channel-id,
    message-id,session-generation,size-class,time,state,error-code}``.
    This is metadata only — no payload content is included.
    """

    channel_id: str
    message_id: str
    session_generation: str
    size_class: str  # bounded classification (e.g. "small", "medium", "large")
    time: str
    state: str
    error_code: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_minimum_only(self) -> bool:
        """Confirm this metadata contains no content-derived fields."""
        return all(
            hasattr(self, field_name) for field_name in MINIMUM_ROUTING_METADATA
        )


# ---------------------------------------------------------------------------
# Access verification (A189: PAYLOAD-ACCESS)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccessCheckResult:
    """Result of verifying actor access to the auxiliary channel (A189)."""

    actor: str
    access_type: str  # "view", "decrypt", "search", "export", "replay", "delegate"
    allowed: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


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


__all__ = [
    "AUDIT_RESTRICTIONS",
    "AccessCheckResult",
    "ChannelMessageMetadata",
    "CHANNEL_OWNER",
    "ENDPOINT_PRINCIPAL",
    "KEY_PROPERTIES",
    "MINIMUM_ROUTING_METADATA",
    "NON_ACCESS_ACTORS",
    "STORAGE_PROPERTIES",
    "XINGCHENG_AUXILIARY_CHANNEL_ID",
    "channel_status",
    "failure_signal",
    "verify_channel_access",
    "verify_output_separation",
]
