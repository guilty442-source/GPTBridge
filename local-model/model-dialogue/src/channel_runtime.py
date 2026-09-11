from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "star-chat"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT", "E:/GPTBridge")).resolve()
TOOL_ROOT = Path(
    os.environ.get("GPTBRIDGE_TOOL_DIR", ROOT / "local-model" / "model-dialogue")
).resolve()
CANONICAL_TOOL_ROOT = (ROOT / "local-model" / "model-dialogue").resolve()
if (
    ROOT != Path("E:/GPTBridge").resolve()
    or TOOL_ROOT != CANONICAL_TOOL_ROOT
    or not TOOL_ROOT.is_dir()
):
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))

from governance_rule.permission_directory.registries.permissions.tool_routes import (  # noqa: E402
    authorize_ai_target,
)
from governance_rule.execution.tool_runtime.governed_runtime import GovernedToolRuntime  # noqa: E402
from star_chat.application.service import StarChatService  # noqa: E402


service = StarChatService()
active_runtime: GovernedToolRuntime | None = None


async def execute(
    command: str,
    payload: dict[str, Any],
    _request_id: str,
) -> tuple[str, dict[str, Any]]:
    requester = str(payload.pop("_governed_requester_actor", ""))
    authorize_ai_target(requester, TOOL_ID, command)
    if not service.owns(command):
        raise PermissionError("PERMISSION_DENIED")
    loop = asyncio.get_running_loop()

    def forward_progress(progress: dict[str, Any]) -> None:
        runtime = active_runtime
        if runtime is None:
            return
        asyncio.run_coroutine_threadsafe(
            runtime.emit(
                _request_id,
                f"{command}_progress",
                {**dict(progress), "request_id": _request_id},
            ),
            loop,
        )

    return await service.handle(
        command,
        payload,
        request_id=_request_id,
        progress_callback=forward_progress if command == "star_chat_send_message" else None,
    )


async def main() -> None:
    global active_runtime
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version=service.VERSION,
        executor=execute,
        startup=service.start,
        shutdown=service.shutdown,
        cancellation=service.cancel_request,
        health=lambda: {
            "service_ready": True,
            "model_service_owner": "xingcheng",
            "settings_owner": "xingcheng",
            "business_layer_owner": "xingcheng",
            "permission_profile": "local-model-platform-v1",
            "cache_owner": "xingcheng",
            "cache_storage": "local-model/runtime/cache/companions/star-chat",
            "backup_owner": "xingcheng",
            "backup_storage": "global-cleaner/data/business/backups/xingcheng",
            "main_system_independent_tool": False,
            "companion_tool": True,
            "companion_owner": "xingcheng",
            "database_shared": True,
            "separate_business_layer": False,
            "separate_settings_layer": False,
            "automatic_workflow": True,
            "primary_language": "zh-TW",
            "workflow_sequence": list(service.AUTOMATIC_WORKFLOW_SEQUENCE),
        },
        channel_modes={"ai": "submit"},
    )
    service.bind_channel(runtime.channel_for("ai"))
    active_runtime = runtime
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
