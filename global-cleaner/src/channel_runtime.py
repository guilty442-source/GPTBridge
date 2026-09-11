from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


TOOL_ID = "global-cleaner"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT", "E:/GPTBridge")).resolve()
TOOL_ROOT = (ROOT / TOOL_ID).resolve()
if ROOT != Path("E:/GPTBridge").resolve() or not TOOL_ROOT.is_dir():
    raise PermissionError("PERMISSION_DENIED")


def _tool_version() -> str:
    try:
        manifest_path = TOOL_ROOT / "manifest.json"
        version = str(
            json.loads(manifest_path.read_text("utf-8")).get("version", "")
        ).strip()
        if version:
            return version
    except Exception:
        pass
    return "1.0.0"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from governance_rule.execution.tool_runtime.governed_runtime import (  # noqa: E402
    GovernedCliExecutor,
    GovernedToolRuntime,
)


executor = GovernedCliExecutor(TOOL_ID, TOOL_ROOT)


async def main() -> None:
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version=_tool_version(),
        executor=executor,
        cancellation=executor.cancel,
        health=lambda: {"service_ready": True},
    )
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
