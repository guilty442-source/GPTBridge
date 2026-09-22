"""Model-Dialogue lightweight governed runtime entry.

Design (governor directive 2026-09-17): the model dialogue must open WITHOUT
starting the local model.  This runtime serves the dialogue UI under the
sealed ``model-dialogue`` identity and never loads model weights: startup
binds the governed AI channel only, and `star_chat_status`/
`star_chat_send_message` reach xingcheng (the model owner) on demand through
the authorized `model-dialogue -> xingcheng` AI route.  When the model is
not running the service answers with the typed `AI_CHANNEL_NOT_CONNECTED`
state and the UI shows "model not ready".
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "model-dialogue"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT")).resolve()
TOOL_ROOT = Path(
    os.environ.get(
        "GPTBRIDGE_TOOL_DIR",
        ROOT / "Standalone tools" / "local-model" / "model-dialogue",
    )
).resolve()
CANONICAL_TOOL_ROOT = (
    ROOT / "Standalone tools" / "local-model" / "model-dialogue"
).resolve()
DIALOGUE_SERVICE_ROOT = (
    TOOL_ROOT / "src" / "backend" / "services"
).resolve()
if (
    not ROOT.is_dir()
    or TOOL_ROOT != CANONICAL_TOOL_ROOT
    or not TOOL_ROOT.is_dir()
    or not DIALOGUE_SERVICE_ROOT.is_dir()
):
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(DIALOGUE_SERVICE_ROOT))

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


def health_payload() -> dict[str, Any]:
    """Model-optional health: the dialogue is ready without model weights."""
    return {
        "service_ready": True,
        "model_service_owner": "xingcheng",
        "model_required_for_startup": False,
        "settings_owner": "model-dialogue",
        "business_layer_owner": "model-dialogue",
        "permission_profile": "local-model-platform-v1",
        "cache_owner": "model-dialogue",
        "cache_storage": "model-dialogue/runtime/cache",
        "backup_owner": "model-dialogue",
        "backup_storage": "system-rescue/data/business/backups/model-dialogue",
        "main_system_independent_tool": True,
        "companion_tool": False,
        "companion_owner": "",
        "database_shared": True,
        "separate_business_layer": False,
        "separate_settings_layer": False,
        "automatic_workflow": True,
        "primary_language": "zh-TW",
        "workflow_sequence": list(service.AUTOMATIC_WORKFLOW_SEQUENCE),
    }


async def main() -> None:
    global active_runtime
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version=service.VERSION,
        executor=execute,
        startup=service.start,
        shutdown=service.shutdown,
        cancellation=service.cancel_request,
        health=health_payload,
        channel_modes={"ai": "submit"},
    )
    service.bind_channel(runtime.channel_for("ai"))
    active_runtime = runtime
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
