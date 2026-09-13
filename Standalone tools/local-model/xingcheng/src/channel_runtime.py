from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "star-chat"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT")).resolve()
TOOL_ROOT = (ROOT / "Standalone tools" / "local-model" / "model-dialogue" / "xingcheng").resolve()
if not ROOT.is_dir() or not TOOL_ROOT.is_dir():
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src"))

from governance_rule.permission_directory.registries.permissions.tool_routes import (  # noqa: E402
    authorize_ai_target,
)
from governance_rule.execution.tool_runtime.governed_runtime import GovernedToolRuntime  # noqa: E402


class StarChatService:
    TOOL_ID = TOOL_ID
    VERSION = "1.0.0"

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = tool_root

    def owns(self, command: str) -> bool:
        return command in {"ai-connections"}

    async def handle(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "ai-connections":
            return "ok", {"connections": "established"}
        raise PermissionError("PERMISSION_DENIED")

    async def start(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


service = StarChatService(TOOL_ROOT)


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
    try:
        runtime = GovernedToolRuntime(
            tool_id=TOOL_ID,
            version=service.VERSION,
            executor=execute,
            startup=service.start,
            shutdown=service.shutdown,
            health=lambda: {
                "service_ready": True,
            },
            channel_modes={"ai": "process"},
        )
        await runtime.run()
    finally:
        pass


if __name__ == "__main__":
    asyncio.run(main())