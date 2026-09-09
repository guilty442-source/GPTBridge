from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "investment-mobile"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT", "E:/GPTBridge")).resolve()
TOOL_ROOT = (ROOT / TOOL_ID).resolve()
MOBILE_ROOT = TOOL_ROOT
if (
    ROOT != Path("E:/GPTBridge").resolve()
    or not TOOL_ROOT.is_dir()
):
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(MOBILE_ROOT / "src" / "backend" / "services"))

from investment_mobile.application.service import InvestmentMobileService  # noqa: E402
from governance_rule.execution.tool_runtime.governed_runtime import GovernedToolRuntime  # noqa: E402


service = InvestmentMobileService(MOBILE_ROOT)


async def execute(
    command: str, payload: dict[str, Any], _request_id: str
) -> tuple[str, dict[str, Any]]:
    requester = str(payload.pop("_governed_requester_actor", ""))
    if requester not in {
        "governance/main-system",
        "governance/tool/investment-mobile",
    }:
        raise PermissionError("PERMISSION_DENIED")
    if not service.owns(command):
        raise PermissionError("PERMISSION_DENIED")
    return await service.handle(command, payload)


async def main() -> None:
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version="1.00000",
        executor=execute,
        startup=service.start,
        shutdown=service.shutdown,
        health=lambda: {
            "service_ready": True,
            "independent_tool": True,
            "canonical_source_root": "investment-mobile",
            "permission_owner": "ai-assistant",
            "permission_profile": "ai-investment-manager-v1",
            "cache_owner": "ai-assistant",
            "cache_storage": "ai-assistant/runtime/cache/companions/investment-mobile",
            "backup_owner": "ai-assistant",
            "backup_storage": "global-cleaner/data/business/backups/ai-assistant",
            "main_system_independent_tool": True,
            "business_layer_owner": "ai-assistant",
            "settings_owner": "ai-assistant",
            "separate_business_layer": False,
            "separate_settings_layer": False,
            "database": "xingcheng-shared-repository",
            "connection_coordinator": "xingcheng",
            "ai_channel_participant": True,
            "ai_channel_scope": "submit-to-xingcheng-only",
        },
        channel_modes={"ai": "submit"},
    )
    service.bind_channel(runtime.channel_for("ai"))
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
