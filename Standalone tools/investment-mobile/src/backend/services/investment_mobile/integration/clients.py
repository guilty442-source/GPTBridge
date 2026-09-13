"""Investment Mobile Integration Layer - governed channel clients.

investment-mobile is a submit-only AI-channel participant: its only
authorized cross-tool route is investment-mobile -> xingcheng carrying the
two ``xingcheng_mobile_*`` commands (see
``tool_routes.authorize_investment_mobile_route``).  All business data and
market access stay owned by ai-assistant behind the xingcheng relay; the
mobile companion never opens a direct network or database connection.
"""

from __future__ import annotations

from typing import Any


MOBILE_ACTOR = "governance/tool/investment-mobile"
XINGCHENG_TOOL_ID = "xingcheng"
SNAPSHOT_COMMAND = "xingcheng_mobile_get_investment_snapshot"
INSTRUCTION_COMMAND = "xingcheng_mobile_submit_investment_instruction"
_TIMEOUT_SECONDS = 60.0


class ChannelClient:
    """Governed AI-channel client bound to the tool runtime's ai channel."""

    def __init__(self, tool_id: str) -> None:
        self.tool_id = str(tool_id or "").strip()
        self._client: Any | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    def bind_channel(self, channel: Any) -> None:
        """Bind the governed 'ai' channel provided by GovernedToolRuntime."""
        from governance_rule.permission_directory.registries.permissions.tool_routes import (
            authorize_investment_mobile_route,
        )
        from shared_layer import GovernedRequestClient

        self._client = GovernedRequestClient(
            channel,
            MOBILE_ACTOR,
            authorize_investment_mobile_route,
            transport="ai-channel",
        )

    async def snapshot(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request(SNAPSHOT_COMMAND, dict(payload or {}))

    async def submit_instruction(self, payload: dict[str, Any]) -> dict[str, Any]:
        request = dict(payload)
        operation = str(request.get("operation") or "").strip()
        if operation != "update_shared_settings" and not str(
            request.get("instruction") or ""
        ).strip():
            request["instruction"] = operation or "status"
        return await self._request(INSTRUCTION_COMMAND, request)

    async def send(
        self, command: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        """Send a mobile-route command through the governed channel."""
        if command == SNAPSHOT_COMMAND:
            result = await self.snapshot(payload)
        elif command == INSTRUCTION_COMMAND:
            result = await self.submit_instruction(payload)
        else:
            return "error", {
                "ok": False,
                "error_code": "COMMAND_NOT_OWNED",
                "command": command,
            }
        return ("ok" if result.get("ok") is not False else "error"), result

    async def _request(self, command: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            return {
                "ok": False,
                "queued": False,
                "error_code": "AI_CHANNEL_NOT_CONNECTED",
                "message": "投資手機版 AI 通道尚未連線。",
            }
        try:
            return await self._client.request(
                XINGCHENG_TOOL_ID,
                command,
                dict(payload),
                timeout_seconds=_TIMEOUT_SECONDS,
            )
        except PermissionError:
            raise
        except Exception as error:
            return {
                "ok": False,
                "queued": False,
                "error_code": "CHANNEL_REQUEST_FAILED",
                "message": str(error),
            }


class ExternalAPIClient:
    """Market data access mediated by the governed channel (no direct network)."""

    def __init__(
        self,
        base_url: str,
        channel_client: ChannelClient | None = None,
    ) -> None:
        self.base_url = base_url
        self._channel_client = channel_client

    async def fetch_market_data(self, symbol: str) -> dict[str, Any]:
        if self._channel_client is None:
            return {
                "ok": False,
                "error_code": "NETWORK_ACCESS_DISABLED",
                "symbol": symbol,
                "data": {},
            }
        result = await self._channel_client.submit_instruction(
            {
                "operation": "market_search",
                "symbols": [str(symbol)],
                "instruction": f"market-search {symbol}",
            }
        )
        return {
            "ok": result.get("ok") is not False,
            "symbol": symbol,
            "data": result,
        }
