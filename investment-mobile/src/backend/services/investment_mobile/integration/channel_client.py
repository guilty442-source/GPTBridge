from __future__ import annotations

from typing import Any

from governance_rule.permission_directory.registries.permissions.tool_routes import (
    MOBILE_ACTOR,
    STAR_TOOL_ID,
    authorize_investment_mobile_route,
)
from shared_layer import GovernedRequestClient


class InvestmentMobileChannelClient:
    """Compatibility contract for the mobile tool's governed AI channel."""

    def __init__(self, channel: Any) -> None:
        self._client = GovernedRequestClient(
            channel,
            MOBILE_ACTOR,
            authorize_investment_mobile_route,
            transport="investment-mobile-channel",
        )

    def request_sync(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float = 30,
    ) -> dict[str, Any]:
        return self._client.request_sync(
            STAR_TOOL_ID,
            command,
            payload,
            timeout_seconds=timeout_seconds,
        )
