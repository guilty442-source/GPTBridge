from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "system-rescue"
ROOT = Path(
    os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT", "E:/GPTBridge")
).resolve()
TOOL_ROOT = (ROOT / "system-rescue").resolve()
if ROOT != Path("E:/GPTBridge").resolve() or not TOOL_ROOT.is_dir():
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))

from governance_rule.execution.tool_runtime.governed_runtime import GovernedToolRuntime  # noqa: E402


async def execute(
    command: str,
    payload: dict[str, Any],
    _request_id: str,
) -> tuple[str, dict[str, Any]]:
    """Execute a governed system-rescue command.

    system-rescue is the authority for audit records, runtime logs, and
    platform packaging integration. All commands are processed under
    governance-authenticated shared-layer authorization.
    """
    if command == "system_health_check":
        return "system_health_check_result", {
            "ok": True,
            "tool_id": TOOL_ID,
            "authority": "main-system-central-packaging-only",
            "resident": False,
        }
    if command == "system_rescue_status":
        return "system_rescue_status_result", {
            "ok": True,
            "tool_id": TOOL_ID,
            "authority": "main-system",
            "channels": ["system"],
            "audit_records_owner": True,
            "runtime_logs_owner": True,
        }
    raise PermissionError("PERMISSION_DENIED")


async def main() -> None:
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version="1.00000",
        executor=execute,
        health=lambda: {
            "service_ready": True,
            "resident": False,
            "authority": "main-system-central-packaging-only",
            "tool_id": TOOL_ID,
        },
        self_repair=False,
    )
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
