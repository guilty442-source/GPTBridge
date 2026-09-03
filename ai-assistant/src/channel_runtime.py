from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "ai-assistant"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT", "E:/GPTBridge")).resolve()
TOOL_ROOT = (ROOT / TOOL_ID).resolve()
if ROOT != Path("E:/GPTBridge").resolve() or not TOOL_ROOT.is_dir():
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))

from investment_network_policy import install_investment_manager_network_policy  # noqa: E402

install_investment_manager_network_policy()

from ai_nexus.application.service import AiNexusService  # noqa: E402
from governance_rule.permission_directory.registries.permissions.tool_routes import (  # noqa: E402
    authorize_ai_target,
)
from shared_layer.governed_runtime import GovernedToolRuntime  # noqa: E402


service = AiNexusService(TOOL_ROOT)


async def execute(
    command: str,
    payload: dict[str, Any],
    _request_id: str,
) -> tuple[str, dict[str, Any]]:
    requester = str(payload.pop("_governed_requester_actor", ""))
    authorize_ai_target(requester, TOOL_ID, command)
    if not service.owns(command):
        raise PermissionError("PERMISSION_DENIED")
    return await service.handle(command, payload)


async def main() -> None:
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version="1.0.0",
        executor=execute,
        startup=service.start,
        shutdown=service.shutdown,
        health=lambda: {
            "service_ready": True,
            "network_policy": "loopback-only",
            "investment_service_owner": "xingcheng",
        },
        channel_modes={"ai": "process"},
    )
    service.ai_connections.bind_channel(runtime.channel_for("ai"))
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
