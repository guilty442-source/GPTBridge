from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


TOOL_ID = "system-rescue"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT", "E:/GPTBridge")).resolve()
TOOL_ROOT = (ROOT / TOOL_ID).resolve()
if ROOT != Path("E:/GPTBridge").resolve() or not TOOL_ROOT.is_dir():
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))

from system_rescue.integration.repair_executor import (  # noqa: E402
    CentralRepairExecutor,
)
from shared_layer.governed_runtime import GovernedToolRuntime  # noqa: E402


executor = CentralRepairExecutor(TOOL_ID, TOOL_ROOT)


async def main() -> None:
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version="1.0.0",
        executor=executor,
        cancellation=executor.cancel,
        health=lambda: {"service_ready": True},
    )
    executor.channel = runtime.channel
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
