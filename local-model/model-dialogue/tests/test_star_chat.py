"""local-model consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "global-cleaner" / "src"),
    str(_ROOT / "ai-assistant" / "src"),
    str(_ROOT / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "vaultly" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

########################################################################
# source: restored_star_chat.py
########################################################################
import json
from pathlib import Path
from typing import Any

import pytest

from governance_rule.permission_directory.registries.permissions.tool_routes import (
    XINGCHENG_AUTOMATIC_WORKFLOW_SEQUENCE,
    authorize_ai_route,
    authorize_xingcheng_automatic_workflow,
    tool_actor,
)
from star_chat.application.service import StarChatService
from shared_layer.channel import SharedLayerChannel


ROOT = Path(__file__).resolve().parents[1]


class FakeClient:
    def __init__(self, response: dict[str, Any] | None = None) -> None:
        self.response = response or {"ok": True, "response": "星澄回覆"}
        self.requests: list[tuple[str, str, dict[str, Any], float]] = []
        self.request_ids: list[str | None] = []
        self.cancelled: list[tuple[str, str]] = []

    async def request(
        self,
        tool_id: str,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float,
        request_id: str | None = None,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        self.requests.append((tool_id, command, payload, timeout_seconds))
        self.request_ids.append(request_id)
        return dict(self.response)

    def cancel(self, tool_id: str, request_id: str) -> bool:
        self.cancelled.append((tool_id, request_id))
        return True


def test_cancellation_poll_uses_process_capability_without_target_claim() -> None:
    channel = object.__new__(SharedLayerChannel)
    channel._channel_id = "ai"
    channel._tool_id = "xingcheng"
    issued: dict[str, Any] = {}

    def issue(**kwargs: Any) -> str:
        issued.update(kwargs)
        return "token"

    class Store:
        def request_cancelled(
            self, token: str, request_id: str, target_tool_id: str
        ) -> bool:
            return (token, request_id, target_tool_id) == (
                "token",
                "request-1",
                "xingcheng",
            )

    channel._issue = issue
    channel._store = Store()

    assert channel.request_cancelled("request-1") is True
    assert issued == {
        "capability": "ai-channel-request-process",
        "action": "claim",
        "target_tool_id": None,
    }


def test_progress_uses_existing_governed_respond_capability() -> None:
    channel = object.__new__(SharedLayerChannel)
    channel._channel_id = "ai"
    channel._tool_id = "xingcheng"
    issued: dict[str, Any] = {}

    def issue(**kwargs: Any) -> str:
        issued.update(kwargs)
        return "token"

    class Store:
        def publish_progress(
            self, token: str, request_id: str, target_tool_id: str, payload: Any
        ) -> bool:
            return (token, request_id, target_tool_id, payload) == (
                "token",
                "request-1",
                "xingcheng",
                {"sequence": 1, "text": "partial"},
            )

    channel._issue = issue
    channel._store = Store()

    assert channel.progress(
        "request-1", {"sequence": 1, "text": "partial"}
    ) is True
    assert issued == {
        "capability": "ai-channel-request-process",
        "action": "respond",
        "target_tool_id": None,
    }


def service_with_client(response: dict[str, Any] | None = None) -> tuple[StarChatService, FakeClient]:
    service = StarChatService()
    client = FakeClient(response)
    service._client = client
    return service, client


def test_manifest_declares_main_system_only_independent_interface() -> None:
    manifest = json.loads((ROOT / "manifest.json").read_text("utf-8"))
    assert manifest["id"] == "star-chat"
    assert manifest["display_version"] == "1.0"
    assert manifest["distribution"] == {
        "mode": "special-unpackaged",
        "package": False,
    }
    connections = manifest["capabilities"]["ai-connections"]
    assert connections["workflow_sequence"] == list(
        XINGCHENG_AUTOMATIC_WORKFLOW_SEQUENCE
    )
    assert connections["service_owner"] == "xingcheng"
    assert manifest["host_tool_id"] == "xingcheng"
    assert manifest["physical_owner_root"] == "local-model"
    assert manifest["canonical_source_root"] == "local-model/model-dialogue"
    assert manifest["entry"] == "local-model/model-dialogue/src/main"
    assert manifest["main_system_independent_tool"] is True
    assert connections["database_shared"] is True
    assert connections["database_owner"] == "xingcheng"
    assert connections["settings_owner"] == "xingcheng"
    assert connections["business_layer_owner"] == "xingcheng"
    assert manifest["shared_cache_owner"] == "xingcheng"
    assert manifest["backup_owner"] == "xingcheng"
    assert manifest["backup_storage"] == (
        "global-cleaner/data/business/backups/xingcheng"
    )
    assert connections["separate_business_layer"] is False
    assert connections["separate_settings_layer"] is False
    assert connections["direct_weight_access"] is False
    assert connections["additional_application_command_restrictions"] is False
    assert connections["investment_manager_direct_access"] is False
    assert connections["modes"] == ["unified-dialogue", "user-programming-commands"]
    assert connections["training_ui"] is False
    assert connections["capability_composition_ui"] is False
    assert connections["native_internal_training_owner"] == "star-main-native-model"
    assert connections["native_internal_capability_owner"] == "star-main-native-model"
    assert connections["ollama_training_route"] == "star-native-internal-only"
    assert connections["selected_star_native_database_access"] == (
        "all-project-databases-excluding-governance-rule"
    )
    assert connections["database_write"] is True
    assert connections["database_actions"] == [
        "read",
        "create-write-save",
        "append",
        "update",
        "execute",
        "delete",
        "rollback",
    ]


def test_renderer_has_chat_only_and_internal_native_management_notice() -> None:
    source = (ROOT / "src" / "ui" / "StarChatWindowApp.tsx").read_text("utf-8")
    socket = (ROOT / "src" / "ui" / "backendSocket.ts").read_text("utf-8")
    styles = (ROOT / "src" / "ui" / "star-chat.css").read_text("utf-8")
    assert "模型對話" in source
    assert "我來教星澄" not in source
    assert "Ollama 模型訓練" not in source
    assert "能力名稱" not in source
    assert "開始討論與投票" not in source
    assert "外部 AI 協作已停用" in source
    assert "訓練與能力編成由星澄原生模型內部自行處理" in source
    assert "等待連線" in source
    assert "star_chat_send_message" in source
    assert "star_chat_submit_teaching_example" not in source
    assert "star_chat_train_with_gpt" not in source
    assert "STAR_CAPABILITY_COMPOSITION_V1" not in source
    assert "選擇生成模型" in source
    assert "const DEFAULT_MODEL = 'gemma4:e2b-it-qat'" in source
    assert "Chat 模式，現在可以開始聊聊" in source
    assert "Coding 模式，準備處理程式任務" in source
    assert "conversation_mode: conversationMode" in source
    assert "previous_conversation_mode: pendingModeTransition" in source
    assert "進階設定" in source
    assert "星澄自動路由模型" not in source
    assert "selectable_models" in source
    assert "runtime_model" in source
    assert "result.generation" in source
    assert "refreshModelStatus" in source
    assert "runtimeStatus.model_installed === true" in source
    assert "ready ? 15_000 : 3_000" in source
    assert "GOVERNED_LOCAL_MODELS" in source
    assert "ReasoningLevel" in source
    assert "ReasoningEffort" in source
    assert "GenerationSpeed" in source
    assert "TaskIntensity" in source
    assert "const [selectedModel, setSelectedModel] = useState('')" in source
    assert "useState<ReasoningLevel>('intermediate')" in source
    assert "useState<GenerationSpeed>('medium')" in source
    assert "useState<TaskIntensity>('normal')" in source
    assert "reasoning_effort: REASONING_EFFORT_BY_LEVEL[reasoningLevel]" in source
    assert "generation_speed: generationSpeed" in source
    assert "task_intensity: taskIntensity" in source
    assert "autonomous_agent: true" in source
    assert "選擇推理等級" in source
    assert "選擇模型反應速度" in source
    for level in ("輕度", "中級", "高高", "超高", "極高"):
        assert f">{level}</option>" in source
    for speed in ("慢速", "低速", "中速", "高速", "超高速"):
        assert f">{speed}</option>" in source
    for intensity in ("簡單", "普通", "中級", "困難"):
        assert f">{intensity}</option>" in source
    for model_name in (
        "star-main-native-model",
        "gemma4:e2b-it-qat",
        "llama3.1:8b-instruct-q4_K_M",
        "qwen3.8:27b-q4_K_M",
        "qwen3.6:35b-a3b-coding",
        "gpt-oss:20b",
        "qwen3:30b-a3b-instruct-2507-q4_K_M",
        "deepseek-r1:8b-0528-qwen3-q4_K_M",
        "qwen3.5:9b-q4_K_M",
        "nemotron-3-nano:4b",
        "qwen2.5-coder:7b",
    ):
        assert model_name in source
    for removed_model_name in (
        "gemma2:9b-instruct-q4_K_M",
        "qwen2-math:7b-instruct-q4_K_M",
        "qwen2.5-coder:7b-instruct-q4_K_M",
        "qwen3.5:4b-q4_K_M",
    ):
        assert removed_model_name not in source
    assert "cancelRequests('star_chat_send_message')" in source
    assert "停止產生回答" in source
    assert "準備處理" in source
    assert "已處理 {thinkingSeconds} 秒" in source
    assert "progressiveChunks" in source
    assert "streamSequenceRef.current += 1" in source
    assert "上下文依本機負載自動調整" in source
    assert "context_budget_characters: hardwareContext.characters" in source
    assert "jump-to-latest" in source
    assert "↓ 回到最新訊息" in source
    assert "命令已理解並執行" in source
    assert "max_output_tokens: maxOutputTokens" in source
    assert "TASK_INTENSITY_OUTPUT_TOKENS[taskIntensity]" in source
    assert "streamed = true" in source
    assert "progress.text" in source
    assert "toolbox_cancel_tool_run" in socket
    assert "${pending.command}_progress" in socket
    assert "pending.progress?.(result || {})" in socket
    assert "pending.command !== command" in socket
    assert "timeoutMessage(command)" in socket
    assert "app:get-backend-session" in socket
    assert "searchParams.set('token', token)" in socket
    assert "html, body, #root { width: 100%; height: 100%; }" in styles
    assert "overflow-y: auto; overscroll-behavior: contain" in styles
    assert ".jump-to-latest" in styles
    assert ".chat-workspace { min-height: 0; overflow: hidden;" in styles


def test_governance_allows_only_declared_star_routes() -> None:
    actor = tool_actor("star-chat")
    assert authorize_ai_route(actor, "xingcheng", "xingcheng_infer") == "star-chat"
    service = StarChatService()
    assert service.owns("star_chat_submit_teaching_example") is False
    assert service.owns("star_chat_train_with_gpt") is False
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(actor, "ai-collaboration", "ai_nexus_send_message")


@pytest.mark.asyncio
async def test_chat_routes_to_star_and_preserves_bounded_history() -> None:
    service, client = service_with_client()
    event, result = await service.handle(
        "star_chat_send_message",
        {
            "message": "請繼續完成程式",
            "runtime_model": "qwen3.8:27b-q4_K_M",
            "reasoning_effort": "high",
            "documents": [{"id": "doc-1", "content": "direct task context"}],
            "history": [
                {"role": "user", "content": "建立 Python API"},
                {"role": "assistant", "content": "已建立規格"},
            ],
        },
    )
    assert event == "star_chat_send_message_result"
    assert result["ok"] is True
    tool_id, command, payload, timeout = client.requests[0]
    assert (tool_id, command) == ("xingcheng", "xingcheng_infer")
    assert "使用者最新訊息：請繼續完成程式" in payload["prompt"]
    assert "星澄：已建立規格" in payload["prompt"]
    assert payload["runtime_model"] == "qwen3.8:27b-q4_K_M"
    assert payload["reasoning_effort"] == "high"
    assert payload["conversation_mode"] == "chat"
    assert payload["interaction_mode"] == "model-dialogue-chat"
    assert payload["autonomous_agent"] is True
    assert payload["automatic_workflow"] is True
    assert payload["primary_language"] == "zh-TW"
    assert payload["workflow_sequence"] == list(
        XINGCHENG_AUTOMATIC_WORKFLOW_SEQUENCE
    )
    authorize_xingcheng_automatic_workflow(
        tool_actor("star-chat"),
        "xingcheng",
        "xingcheng_infer",
        payload,
    )
    assert payload["documents"] == [
        {"id": "doc-1", "content": "direct task context"}
    ]
    assert payload["_runtime_model_selection_authorized"] is True
    assert result["channel_workflow"]["automatic"] is True
    assert timeout == 600


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["chat", "coding"])
async def test_conversation_mode_changes_prompt_and_folder_scope(mode: str) -> None:
    service, client = service_with_client()

    await service.handle(
        "star_chat_send_message",
        {
            "message": "請協助我",
            "conversation_mode": mode,
            "programming_folder": "C:/workspace/project",
        },
    )

    forwarded = client.requests[0][2]
    assert forwarded["conversation_mode"] == mode
    assert forwarded["interaction_mode"] == f"model-dialogue-{mode}"
    assert f"目前是 {'Chat' if mode == 'chat' else 'Coding'} 模式" in forwarded["prompt"]
    assert forwarded["programming_folder"] == (
        "C:/workspace/project" if mode == "coding" else ""
    )


@pytest.mark.asyncio
async def test_mode_transition_notifies_model_before_latest_message() -> None:
    service, client = service_with_client()

    await service.handle(
        "star_chat_send_message",
        {
            "message": "接著處理這件事",
            "conversation_mode": "coding",
            "previous_conversation_mode": "chat",
        },
    )

    prompt = client.requests[0][2]["prompt"]
    assert prompt.startswith("模式切換通知：使用者已從 Chat 切換至 Coding")
    assert prompt.index("模式切換通知") < prompt.index("使用者最新訊息")


def test_context_length_is_bounded_for_local_hardware_load() -> None:
    history = [
        {"role": "user", "content": f"訊息 {index}: " + ("字" * 1_000)}
        for index in range(12)
    ]

    constrained = StarChatService._conversation_context(
        {"history": history, "context_budget_characters": 6_000}
    )
    excessive = StarChatService._conversation_context(
        {"history": history, "context_budget_characters": 999_999}
    )

    assert len(constrained) <= 6_000
    assert len(excessive) <= 24_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reasoning_level", "expected_effort"),
    [
        ("light", "low"),
        ("intermediate", "medium"),
        ("high-high", "high"),
        ("ultra-high", "high"),
        ("extreme", "high"),
    ],
)
async def test_chat_maps_five_reasoning_levels_without_changing_model_assignment(
    reasoning_level: str, expected_effort: str
) -> None:
    service, client = service_with_client()

    await service.handle(
        "star_chat_send_message",
        {
            "message": "測試推理等級",
            "runtime_model": "gemma4:e2b-it-qat",
            "reasoning_level": reasoning_level,
            "generation_speed": "medium",
            "task_intensity": "normal",
        },
    )

    payload = client.requests[0][2]
    assert payload["runtime_model"] == "gemma4:e2b-it-qat"
    assert payload["reasoning_level"] == reasoning_level
    assert payload["reasoning_effort"] == expected_effort


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "task_intensity",
        "generation_speed",
        "expected_tokens",
        "expected_path",
        "expected_strategy",
    ),
    [
        ("simple", "ultra", 128, "single-model-fast-path", "fast-local-preference"),
        ("normal", "medium", 512, "balanced-specialist-path", "balanced-intent-routing"),
        (
            "intermediate",
            "high",
            576,
            "specialist-reasoning-path",
            "intent-specialist-routing",
        ),
        (
            "difficult",
            "slow",
            1_280,
            "full-governed-multistage-path",
            "full-pipeline-routing",
        ),
    ],
)
async def test_task_intensity_and_response_speed_control_output_and_strategy(
    task_intensity: str,
    generation_speed: str,
    expected_tokens: int,
    expected_path: str,
    expected_strategy: str,
) -> None:
    service, client = service_with_client()

    await service.handle(
        "star_chat_send_message",
        {
            "message": "測試任務策略",
            "reasoning_level": "intermediate",
            "generation_speed": generation_speed,
            "task_intensity": task_intensity,
        },
    )

    payload = client.requests[0][2]
    assert payload["max_output_tokens"] == expected_tokens
    assert payload["generation_speed"] == generation_speed
    assert payload["task_intensity"] == task_intensity
    assert payload["reasoning_path"] == expected_path
    assert payload["model_selection_strategy"] == expected_strategy


@pytest.mark.asyncio
async def test_chat_cancellation_propagates_to_nested_xingcheng_request() -> None:
    service, client = service_with_client()
    service._active_nested_requests["outer-request"] = "nested-request"

    assert await service.cancel_request("outer-request") is True
    assert client.cancelled == [("xingcheng", "nested-request")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "removed_command",
    ["star_chat_submit_teaching_example", "star_chat_train_with_gpt"],
)
async def test_removed_training_commands_are_denied(removed_command: str) -> None:
    service, client = service_with_client()

    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        await service.handle(removed_command, {})

    assert client.requests == []


@pytest.mark.asyncio
async def test_capability_envelope_is_only_plain_conversation_text() -> None:
    service, client = service_with_client()
    message = '[[STAR_CAPABILITY_COMPOSITION_V1]]{"capability_name":"測試"}'

    event, result = await service.handle(
        "star_chat_send_message",
        {"message": message},
    )

    assert event == "star_chat_send_message_result"
    assert result["ok"] is True
    tool_id, command, payload, timeout = client.requests[0]
    assert (tool_id, command) == ("xingcheng", "xingcheng_infer")
    assert message in payload["prompt"]
    assert "capability_composition" not in payload
    assert timeout == 600


@pytest.mark.parametrize("case_index", range(3_000))
def test_conversation_instruction_transport_matrix(case_index: int) -> None:
    payload = {
        "message": f"第 {case_index} 個指令：只檢查，不要修改，版本維持 1.0",
        "history": [
            {"role": "system", "content": "不可傳遞的系統內容"},
            {"role": "user", "content": f"前置需求 {case_index}"},
        ],
    }
    prompt = StarChatService._conversation_prompt(payload)
    assert f"第 {case_index} 個指令" in prompt
    assert f"前置需求 {case_index}" in prompt
    assert "不可傳遞的系統內容" not in prompt

