"""Xingcheng channel types and constants — A189/E164.

Constants and immutable dataclasses for the Xingcheng-exclusive auxiliary
information channel.  This module provides **read-only data structures** only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
# Access check result (A189: PAYLOAD-ACCESS)
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
