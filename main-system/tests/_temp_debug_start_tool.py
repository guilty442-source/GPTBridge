"""Temporary debug script to test starting a tool."""
import asyncio
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
    tool_id = sys.argv[1] if len(sys.argv) > 1 else "system-rescue"
    print(f"Starting tool: {tool_id}")
    result = await service.start_tool({
        "tool_id": tool_id,
        "request_id": f"debug-start-{tool_id}",
        "background": True,
    })
    print(f"Result: {result}")


if __name__ == "__main__":
    asyncio.run(main())
