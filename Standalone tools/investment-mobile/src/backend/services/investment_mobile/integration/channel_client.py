"""Investment Mobile Integration - Channel Client.

Compatibility module: the real governed-channel implementations live in
``.clients``; this module re-exports them so both import paths stay valid.
"""

from __future__ import annotations

from .clients import (
    INSTRUCTION_COMMAND,
    MOBILE_ACTOR,
    SNAPSHOT_COMMAND,
    XINGCHENG_TOOL_ID,
    ChannelClient,
    ExternalAPIClient,
)

__all__ = [
    "ChannelClient",
    "ExternalAPIClient",
    "INSTRUCTION_COMMAND",
    "MOBILE_ACTOR",
    "SNAPSHOT_COMMAND",
    "XINGCHENG_TOOL_ID",
]
