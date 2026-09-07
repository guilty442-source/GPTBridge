from __future__ import annotations

from typing import Any, Final


MOBILE_PLATFORM_NAME: Final[str] = "AI投資管家原生手機共用平台"
MOBILE_PLATFORM_CONTRACT_VERSION: Final[int] = 1
MOBILE_PLATFORM_CAPABILITIES: Final[tuple[str, ...]] = (
    "portfolio_overview",
    "holdings_read",
    "risk_alerts_read",
    "action_plan_read",
    "xingcheng_command_queue",
)


def mobile_platform_contract() -> dict[str, Any]:
    return {
        "name": MOBILE_PLATFORM_NAME,
        "contract_version": MOBILE_PLATFORM_CONTRACT_VERSION,
        "clients": ["desktop", "android_native"],
        "transport": "paired_json_api",
        "source_of_truth": "desktop_shared_repository",
        "shared_repository": True,
        "business_layer_owner": "ai-assistant",
        "settings_owner": "ai-assistant",
        "permission_profile": "ai-investment-manager-v1",
        "separate_mobile_business_layer": False,
        "separate_mobile_settings_layer": False,
        "mobile_write_scope": "queue_xingcheng_command_only",
        "foreground_sync_seconds": 2,
        "upgrade_compatibility": {
            "mode": "independent-tool",
            "connection_coordinator": "xingcheng",
            "source_tool": "ai-assistant",
        },
        "capabilities": list(MOBILE_PLATFORM_CAPABILITIES),
        "routes": {
            "platform": "/api/platform",
            "pair": "/api/pair",
            "state": "/api/state",
            "xingcheng_command": "/api/xingcheng-command",
        },
    }


__all__ = ["mobile_platform_contract"]
