from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "vaultly"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT", "E:/GPTBridge")).resolve()
TOOL_ROOT = (ROOT / TOOL_ID).resolve()
if ROOT != Path("E:/GPTBridge").resolve() or not TOOL_ROOT.is_dir():
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))

from governance_rule.execution.tool_runtime.governed_runtime import GovernedToolRuntime  # noqa: E402
from vaultly.application.service import VaultlyService  # noqa: E402


service = VaultlyService(TOOL_ROOT)


async def execute(
    command: str,
    payload: dict[str, Any],
    _request_id: str,
) -> tuple[str, dict[str, Any]]:
    if not service.owns(command):
        raise PermissionError("PERMISSION_DENIED")
    return await service.handle(command, payload)


async def main() -> None:
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version=service.VERSION,
        executor=execute,
        startup=service.start,
        shutdown=service.shutdown,
        health=lambda: {"service_ready": True},
    )
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
