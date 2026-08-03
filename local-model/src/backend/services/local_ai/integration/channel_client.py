from __future__ import annotations

from typing import Any


def build_star_ai_channel_client(channel: Any) -> Any:
    """Build Star's only governed cross-tool client."""

    from governance_rule.permission_directory.registries.permissions.tool_routes import (
        authorize_ai_route,
        tool_actor,
    )
    from shared_layer import GovernedRequestClient

    return GovernedRequestClient(
        channel,
        tool_actor("local-ai"),
        authorize_ai_route,
        transport="ai-channel",
    )


__all__ = ["build_star_ai_channel_client"]
