from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path


TOOL_ID = "file-sorter"
EXPECTED_TOOL_ROOT = Path(__file__).resolve().parents[1]
ROOT = Path(
    os.environ.get(
        "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT",
        str(EXPECTED_TOOL_ROOT.parent),
    )
).resolve()
TOOL_ROOT = (ROOT / "Standalone tools" / TOOL_ID).resolve()
if (
    not ROOT.is_dir()
    or not TOOL_ROOT.is_dir()
    or TOOL_ROOT != EXPECTED_TOOL_ROOT
    or TOOL_ROOT.parent != (ROOT / "Standalone tools")
):
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))

from file_sorter.application.service import FileSorterService  # noqa: E402
from governance_rule.execution.tool_runtime.governed_runtime import (  # noqa: E402
    GovernedCliExecutor,
    GovernedToolRuntime,
)


service = FileSorterService(TOOL_ROOT)
executor = GovernedCliExecutor(TOOL_ID, TOOL_ROOT)


async def main() -> None:
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version=service.VERSION,
        executor=executor,
        startup=service.start,
        shutdown=service.shutdown,
        cancellation=executor.cancel,
        health=service.health_snapshot,
    )
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
