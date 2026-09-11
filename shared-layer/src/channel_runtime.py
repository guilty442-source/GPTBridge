from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "shared-layer"
ROOT = Path(__file__).resolve().parents[2]
TOOL_ROOT = ROOT / "shared-layer"


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
sys.path.insert(0, str(TOOL_ROOT / "src"))

from governance_rule.execution.tool_runtime.governed_runtime import GovernedToolRuntime  # noqa: E402


async def execute(
    command: str,
    payload: dict[str, Any],
    _request_id: str,
) -> tuple[str, dict[str, Any]]:
    del payload, _request_id
    if command != "shared_layer_status":
        raise PermissionError("PERMISSION_DENIED")
    return "shared_layer_status_result", {
        "ok": True,
        "resident": True,
        "authority": "governance_rule",
        "channels": ["system", "ai"],
    }


async def main() -> None:
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version=_tool_version(),
        executor=execute,
        health=lambda: {
            "service_ready": True,
            "resident": True,
            "authority": "governance_rule",
        },
    )
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
