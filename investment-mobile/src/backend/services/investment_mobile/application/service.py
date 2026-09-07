from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..domain.platform_contract import mobile_platform_contract
from ..integration.channel_client import InvestmentMobileChannelClient
from ..presentation.mobile_gateway import MobileSyncGateway


class InvestmentMobileService:
    """Independent mobile lifecycle backed by the investment manager's shared state."""

    COMMANDS = {
        "investment_mobile_status",
        "investment_mobile_start",
        "investment_mobile_stop",
        "investment_mobile_rotate_pairing",
    }

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve()
        self._client: Any | None = None
        self._shared_settings: dict[str, Any] = {
            "enabled": False,
            "allow_lan": False,
            "port": 18765,
        }
        self.gateway = MobileSyncGateway(
            self.tool_root,
            self._snapshot,
            self._submit_instruction,
            lambda: "",
        )

    def bind_channel(self, channel: Any) -> None:
        self._client = InvestmentMobileChannelClient(channel)

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    def _snapshot(self) -> dict[str, Any]:
        if self._client is None:
            return {"ok": False, "message": "本地模型服務尚未連線。", "queued": False}
        result = self._client.request_sync(
            "xingcheng_mobile_get_investment_snapshot", {}, timeout_seconds=30
        )
        result["mobile_tool"] = "investment-mobile"
        result["connection_coordinator"] = "xingcheng"
        return result

    def _submit_instruction(self, instruction: str) -> dict[str, Any]:
        if self._client is None:
            return {"ok": False, "message": "本地模型服務尚未連線。", "queued": False}
        return self._client.request_sync(
            "xingcheng_mobile_submit_investment_instruction",
            {"instruction": str(instruction or "").strip()},
            timeout_seconds=45,
        )

    def _refresh_shared_settings(self) -> dict[str, Any]:
        if self._client is None:
            return dict(self._shared_settings)
        try:
            snapshot = self._client.request_sync(
                "xingcheng_mobile_get_investment_snapshot", {}, timeout_seconds=30
            )
        except Exception:
            return dict(self._shared_settings)
        sync = snapshot.get("sync")
        if isinstance(sync, dict):
            self._shared_settings = {
                "enabled": sync.get("enabled") is True,
                "allow_lan": sync.get("allow_lan") is True,
                "port": int(sync.get("port") or 18765),
            }
        return dict(self._shared_settings)

    def _update_shared_settings(self, **settings: Any) -> dict[str, Any]:
        if self._client is None:
            return {"ok": False, "message": "本地模型服務尚未連線。"}
        result = self._client.request_sync(
            "xingcheng_mobile_submit_investment_instruction",
            {"operation": "update_shared_settings", "settings": settings},
            timeout_seconds=30,
        )
        sync = result.get("sync")
        if isinstance(sync, dict):
            self._shared_settings = {
                "enabled": sync.get("enabled") is True,
                "allow_lan": sync.get("allow_lan") is True,
                "port": int(sync.get("port") or 18765),
            }
        return result

    def status(self) -> dict[str, Any]:
        shared_settings = self._refresh_shared_settings()
        status = self.gateway.status(expose_pairing_code=True)
        return {
            "ok": True,
            "tool": "AI 投資管家手機版",
            "tool_id": "investment-mobile",
            "platform": mobile_platform_contract(),
            "connection_coordinator": "xingcheng",
            "direct_ai_assistant_connection": False,
            "ai_channel_participant": True,
            "ai_channel_scope": "submit-to-xingcheng-only",
            "governance_authority": "governance-rule",
            "main_system_independent_tool": True,
            "business_layer_owner": "ai-assistant",
            "settings_owner": "ai-assistant",
            "permission_profile": "ai-investment-manager-v1",
            "cache_owner": "ai-assistant",
            "cache_storage": "ai-assistant/runtime/cache/companions/investment-mobile",
            "backup_owner": "ai-assistant",
            "backup_storage": "global-cleaner/data/business/backups/ai-assistant",
            "separate_business_layer": False,
            "separate_settings_layer": False,
            "gateway": status,
            "shared_settings": shared_settings,
            "database": "ai-assistant-shared-repository",
        }

    async def start(self) -> None:
        settings = self._refresh_shared_settings()
        enabled_env = str(os.environ.get("GPTBRIDGE_INVESTMENT_MOBILE_ENABLED") or "").casefold()
        enabled = enabled_env in {"1", "true", "yes", "on"} or settings["enabled"] is True
        if not enabled:
            return
        allow_lan_env = str(
            os.environ.get("GPTBRIDGE_INVESTMENT_MOBILE_ALLOW_LAN") or ""
        ).casefold()
        allow_lan = (
            allow_lan_env in {"1", "true", "yes", "on"}
            or settings["allow_lan"] is True
        )
        port_value = str(os.environ.get("GPTBRIDGE_INVESTMENT_MOBILE_PORT") or "")
        port = int(port_value) if port_value.isdigit() else int(settings["port"])
        self.gateway.start(port=port, allow_lan=allow_lan)

    async def shutdown(self) -> None:
        self.gateway.shutdown()

    async def handle(
        self, command: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if command == "investment_mobile_status":
            return f"{command}_result", self.status()
        if command == "investment_mobile_start":
            allow_lan = payload.get("allow_lan") is True
            port = int(payload.get("port") or 18765)
            self.gateway.start(port=port, allow_lan=allow_lan)
            self._update_shared_settings(enabled=True, allow_lan=allow_lan, port=port)
            return f"{command}_result", self.status()
        if command == "investment_mobile_stop":
            self.gateway.shutdown()
            self._update_shared_settings(enabled=False)
            return f"{command}_result", self.status()
        if command == "investment_mobile_rotate_pairing":
            self.gateway.rotate_pairing_code()
            return f"{command}_result", self.status()
        raise PermissionError("PERMISSION_DENIED")
