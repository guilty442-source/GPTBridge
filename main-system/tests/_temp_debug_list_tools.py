"""Temporary debug script to list tools and check launchability."""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = ROOT / "main-system" / "src-core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))
SHARED = ROOT / "shared-layer" / "src"
if str(SHARED) not in sys.path:
    sys.path.insert(0, str(SHARED))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tasks.toolbox_service import ToolboxService


class GovernanceStub:
    def authorize_tool_lifecycle(self, *a):
        pass

    def create_tool_governance_bootstrap(self, t):
        return "governed-bootstrap"

    def can_start_tool(self, t):
        return True


async def main():
    service = ToolboxService(ROOT, governance=GovernanceStub())
    result = await service.list_tools()
    tools = result["tools"]
    for t in tools:
        print(
            f"{t['id']:20s} "
            f"enabled={t.get('enabled')} "
            f"perm_denied={t.get('permission_denied')} "
            f"runtime_avail={t.get('runtime_available')} "
            f"exec_exists={t.get('executable_exists')} "
            f"runtime_mode={t.get('runtime_mode')} "
            f"lifecycle_locked={t.get('lifecycle_locked')} "
            f"hidden={t.get('hidden_from_toolbox')} "
            f"status={t.get('status')} "
            f"has_custom_ui={t.get('has_custom_ui')}"
        )


if __name__ == "__main__":
    asyncio.run(main())
