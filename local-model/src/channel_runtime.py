from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any


TOOL_ID = "xingcheng"
ROOT = Path(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT", "E:/GPTBridge")).resolve()
TOOL_ROOT = (ROOT / "local-model").resolve()
if ROOT != Path("E:/GPTBridge").resolve() or not TOOL_ROOT.is_dir():
    raise PermissionError("PERMISSION_DENIED")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))
sys.path.insert(
    0,
    str(
        TOOL_ROOT
        / "model-dialogue"
        / "src"
        / "backend"
        / "services"
    ),
)

from xingcheng.application.service import LocalAiService  # noqa: E402
from star_chat.application.service import StarChatService  # noqa: E402
from governance_rule.permission_directory.registries.permissions.tool_routes import (  # noqa: E402
    authorize_ai_target,
    authorize_xingcheng_automatic_workflow,
    authorize_investment_mobile_target,
    tool_actor,
)
from shared_layer.governed_runtime import GovernedToolRuntime  # noqa: E402


service = LocalAiService(TOOL_ROOT, enable_transformer=True)
dialogue_service = StarChatService()
dialogue_service.bind_local_service(service)
progress_channel: Any = None
active_runtime: GovernedToolRuntime | None = None


async def execute(
    command: str,
    payload: dict[str, Any],
    _request_id: str,
) -> tuple[str, dict[str, Any]]:
    loop = asyncio.get_running_loop()

    def forward_dialogue_progress(progress: dict[str, Any]) -> None:
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

    cancel_event = service.begin_request(_request_id)
    payload["_cancel_event"] = cancel_event
    if progress_channel is not None:
        payload["_progress_callback"] = lambda progress: progress_channel.progress(
            _request_id, progress
        )
    try:
        requester = str(payload.pop("_governed_requester_actor", ""))
        if dialogue_service.owns(command):
            if requester == tool_actor("star-chat"):
                authorize_ai_target(requester, "star-chat", command)
            elif requester in {tool_actor(TOOL_ID), "governance/main-system"}:
                authorize_ai_target(requester, TOOL_ID, command)
            else:
                raise PermissionError("PERMISSION_DENIED")
            return await dialogue_service.handle(
                command,
                payload,
                request_id=_request_id,
                progress_callback=(
                    forward_dialogue_progress
                    if command == "star_chat_send_message"
                    else None
                ),
            )
        if command == "xingcheng_infer":
            payload["autonomous_agent"] = True
            payload["automatic_workflow"] = True
            payload["workflow_sequence"] = list(
                LocalAiService.AUTOMATIC_WORKFLOW_SEQUENCE
            )
            payload["primary_language"] = "zh-TW"
            payload["language_priority"] = "traditional-chinese-taiwan-first"
            payload["translate_before_intent"] = True
        if requester == "governance/tool/investment-mobile":
            authorize_investment_mobile_target(requester, TOOL_ID, command)
        elif command == "xingcheng_infer":
            authorize_xingcheng_automatic_workflow(
                requester, TOOL_ID, command, payload
            )
        else:
            authorize_ai_target(requester, TOOL_ID, command)
        if (
            requester == "governance/tool/ai-assistant"
            and command == "xingcheng_search_investments"
        ):
            payload["allow_external_fallback"] = False
        if requester == "governance/tool/star-chat" and command == "xingcheng_infer":
            payload["_runtime_model_selection_authorized"] = True
        if not service.owns(command):
            raise PermissionError("PERMISSION_DENIED")
        return await service.handle(command, payload)
    finally:
        service.finish_request(_request_id)


async def main() -> None:
    global active_runtime, progress_channel
    runtime = GovernedToolRuntime(
        tool_id=TOOL_ID,
        version="1.0.0",
        executor=execute,
        startup=service.start,
        shutdown=service.shutdown,
        health=lambda: {
            **service.runtime_health(),
            "channel_top_level": True,
            "automatic_workflow": True,
            "primary_language": "zh-TW",
        },
        idle_cleanup=service.release_idle_resources,
        cancellation=service.cancel_request,
        channel_modes={"ai": "process"},
    )
    service.bind_channel(runtime.channel_for("ai"))
    progress_channel = runtime.channel_for("ai")
    active_runtime = runtime
    await runtime.run()


if __name__ == "__main__":
    asyncio.run(main())
