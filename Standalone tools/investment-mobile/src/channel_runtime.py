from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "investment-mobile"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT")).resolve()
TOOL_ROOT = (ROOT / "Standalone tools" / TOOL_ID).resolve()
if not ROOT.is_dir() or not TOOL_ROOT.is_dir():
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))

from investment_mobile.integration.clients import ChannelClient  # noqa: E402
from governance_rule.execution.tool_runtime.governed_runtime import GovernedToolRuntime  # noqa: E402


# Inbound requests arrive on the system channel from governance.  Peers can
# never target investment-mobile: it is a submit-only AI-channel participant
# whose sole route is investment-mobile -> xingcheng.
_GOVERNANCE_MAIN_ACTOR = "governance/main-system"
_SELF_ACTOR = f"governance/tool/{TOOL_ID}"
_AUTHORIZED_REQUESTERS = frozenset({_GOVERNANCE_MAIN_ACTOR, _SELF_ACTOR})

# Codex canonical commands (investment-mobile domain) plus the legacy
# capability aliases declared in manifest.capabilities.
_SNAPSHOT_COMMANDS = frozenset(
    {
        "investment-analysis",
        "investment-mobile-get-snapshot",
    }
)
_INSTRUCTION_COMMANDS = frozenset(
    {
        "investment-manager",
        "investment-market-search",
        "investment-mobile-submit-instruction",
        "investment-mobile-rotate-pairing",
    }
)
_LOCAL_COMMANDS = frozenset(
    {
        "investment-mobile-status",
        "investment-mobile-start",
        "investment-mobile-stop",
    }
)


class InvestmentMobileService:
    TOOL_ID = TOOL_ID
    VERSION = "1.0.0"

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = tool_root
        self.channel = ChannelClient(TOOL_ID)
        self._started = False

    def owns(self, command: str) -> bool:
        return (
            command in _SNAPSHOT_COMMANDS
            or command in _INSTRUCTION_COMMANDS
            or command in _LOCAL_COMMANDS
        )

    def bind_channel(self, channel: Any) -> None:
        self.channel.bind_channel(channel)

    async def handle(
        self, command: str, payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if command in _LOCAL_COMMANDS:
            return f"{command}_result", self._status(command)
        if command in _SNAPSHOT_COMMANDS:
            result = await self.channel.snapshot(dict(payload))
            return f"{command}_result", result
        if command in _INSTRUCTION_COMMANDS:
            result = await self.channel.submit_instruction(dict(payload))
            return f"{command}_result", result
        raise PermissionError("PERMISSION_DENIED")

    def _status(self, command: str) -> dict[str, Any]:
        return {
            "ok": True,
            "tool_id": TOOL_ID,
            "command": command,
            "started": self._started,
            "channel_connected": self.channel.connected,
            "transport": "governance-authenticated-shared-layer",
            "lifecycle_owner": "governed-runtime",
            "business_layer_owner": "ai-assistant",
            "permission_profile": "ai-investment-manager-v1",
            "relay_chain": "investment-mobile -> xingcheng -> ai-assistant",
        }

    async def start(self) -> None:
        self._started = True

    async def shutdown(self) -> None:
        self._started = False


service = InvestmentMobileService(TOOL_ROOT)


async def execute(
    command: str,
    payload: dict[str, Any],
    _request_id: str,
) -> tuple[str, dict[str, Any]]:
    requester = str(payload.pop("_governed_requester_actor", ""))
    if requester not in _AUTHORIZED_REQUESTERS:
        raise PermissionError("PERMISSION_DENIED")
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
        health=lambda: {
            "service_ready": True,
            "channel_connected": service.channel.connected,
            "ai_channel_mode": "submit-only",
        },
        channel_modes={"ai": "submit"},
    )
    service.bind_channel(runtime.channel_for("ai"))
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
