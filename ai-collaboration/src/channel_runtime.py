from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "ai-collaboration"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT", "E:/GPTBridge")).resolve()
TOOL_ROOT = (ROOT / TOOL_ID).resolve()
if ROOT != Path("E:/GPTBridge").resolve() or not TOOL_ROOT.is_dir():
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))

from ai_collaboration.application.service import AiCollaborationService  # noqa: E402
from governance_rule.permission_directory.registries.permissions.tool_routes import (  # noqa: E402
    authorize_ai_target,
)
from governance_rule.execution.tool_runtime.governed_runtime import GovernedToolRuntime  # noqa: E402


service = AiCollaborationService(TOOL_ROOT)


async def execute(
    command: str,
    payload: dict[str, Any],
    _request_id: str,
) -> tuple[str, dict[str, Any]]:
    requester = str(payload.pop("_governed_requester_actor", ""))
    authorize_ai_target(requester, TOOL_ID, command)
    payload["_authorized_requester_actor"] = requester
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
        health=lambda: {"service_ready": True},
        channel_modes={"ai": "process"},
    )
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
