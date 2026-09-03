from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))

from xingcheng.application.service import LocalAiService
from xingcheng.infrastructure.transformer_runtime import StarTransformerRuntime


def test_command_understanding_can_use_fast_chinese_model(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    class Runtime:
        INTENT_MODEL_PREFERENCES = {"conversation": ()}
        TASK_LEVEL_LABELS = {"simple": "simple", "normal": "normal"}

        def generate(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(kwargs)
            return {
                "ok": True,
                "text": json.dumps(
                    {
                        "intents": ["conversation"],
                        "task_intensity": "simple",
                    }
                ),
            }

    service = object.__new__(LocalAiService)
    service.transformer_runtime = Runtime()
    service._command_understanding_cache = {}
    service._command_understanding_cache_lock = threading.Lock()

    result = service._understand_command_with_qwen(
        "請快速回答你好",
        programming_folder=str(tmp_path),
        understanding_model=service.FAST_COMMAND_UNDERSTANDING_MODEL,
    )

    assert result["ok"] is True
    assert calls[0]["requested_model"] == "openbmb/minicpm-v4.6:q8_0"
    assert result["plan"]["command_understanding"]["model"] == (
        "openbmb/minicpm-v4.6:q8_0"
    )


def test_star_business_service_and_local_model_platform_roles_are_separate() -> None:
    manifest = json.loads((ROOT / "local-model" / "manifest.json").read_text("utf-8"))
    locale = json.loads(
        (ROOT / "local-model" / "locales" / "zh-TW.json").read_text("utf-8")
    )

    assert manifest["assistant_identity"] == "星澄"
    assert manifest["tool_display_name"] == "本地模型"
    assert manifest["operation_mode"] == "context-aware-multitask-model-platform"
    assert manifest["ai_entry_gateway"] == "all-ai-business-entries"
    assert manifest["star_native_model_authority"] == (
        "highest-permission-under-governance-rule-central-data-management"
    )
    assert manifest["authorization_owner"] == "governance_rule"
    assert manifest["highest_authority_management_required"] is True
    assert manifest["star_has_fixed_responsibilities"] is True
    assert manifest["star_fixed_responsibilities"] == [
        "sql-central-management",
        "rag-central-management",
        "git-central-management",
    ]
    assert "governance-authority-snapshot" in manifest["permissions"]["allow_read"]
    assert "permission-directory-snapshot" in manifest["permissions"]["allow_read"]
    assert manifest["capabilities"]["xingcheng"]["star_native_model_permissions"][
        "governance_source_access"
    ] == "direct-read-only-authoritative"
    assert manifest["star_permission_activation"] == (
        "governance-rule-explicit-authorization-only"
    )
    platform = manifest["local_model_platform"]
    assert platform["id"] == "local-model-platform"
    assert platform["display_name_zh_tw"] == "本地模型"
    assert platform["all_local_model_execution_owner"] is True
    assert platform["star_direct_model_operation"] is False
    assert manifest["permissions"]["profile"] == "local-model-platform-v1"
    assert "governance-database" in manifest["permissions"]["deny"]
    assert {item["id"] for item in manifest["platform_labels"]} == {
        "ai-entry-gateway",
        "context-multitask",
        "local-model-routing",
        "selected-model-direct",
        "ollama-loopback",
        "governance-controlled",
    }
    assert manifest["capabilities"]["xingcheng"]["platform_mode"] == (
        "context-aware-multitask-services"
    )
    assert locale["tool.name"] == "本地模型"
    assert locale["tool.window_title"] == "本地模型"


def test_star_reads_governance_as_direct_authoritative_source() -> None:
    source = LocalAiService._governance_source_status()

    assert source["connected"] is True
    assert source["mode"] == "direct-read-only"
    assert source["authoritative"] is True
    assert source["source_priority"] == "highest"
    assert source["write_allowed"] is False
    assert source["execute_allowed"] is False
    assert source["bypass_allowed"] is False


class FakeOllamaTransport:
    def __init__(
        self,
        response_text: str = "我是星澄的本機 Transformer 語言模型。",
        models: list[dict[str, Any]] | None = None,
    ) -> None:
        self.response_text = response_text
        self.models = models or [{"name": StarTransformerRuntime.MODEL}]
        self.calls: list[tuple[str, str, dict[str, Any] | None, float]] = []

    def __call__(
        self,
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        self.calls.append((method, url, payload, timeout))
        if url.endswith("/api/tags"):
            return {"models": self.models}
        if url.endswith("/api/version"):
            return {"version": "test"}
        if url.endswith("/api/chat"):
            return {
                "message": {"role": "assistant", "content": self.response_text},
                "prompt_eval_count": 120,
                "eval_count": 18,
                "load_duration": 10,
                "total_duration": 20,
            }
        if url.endswith("/api/embed"):
            inputs = payload.get("input") if isinstance(payload, dict) else []
            return {
                "embeddings": [
                    [1.0, float(index + 1)]
                    for index, _ in enumerate(inputs if isinstance(inputs, list) else [])
                ]
            }
        raise AssertionError(f"unexpected URL: {url}")


def test_transformer_runtime_rejects_non_loopback_endpoint() -> None:
    with pytest.raises(ValueError, match="TRANSFORMER_ENDPOINT_MUST_BE_LOOPBACK"):
        StarTransformerRuntime(enabled=True, endpoint="https://example.com")


def test_transformer_generation_honors_pre_cancelled_request() -> None:
    transport = FakeOllamaTransport()
    runtime = StarTransformerRuntime(enabled=True, transport=transport)
    cancelled = threading.Event()
    cancelled.set()

    result = runtime.generate(
        prompt="停止生成",
        intent="conversation",
        model_role="daily-primary",
        output={"response": ""},
        cancel_event=cancelled,
    )

    assert result["ok"] is False
    assert result["error_code"] == "TRANSFORMER_REQUEST_CANCELLED"
    assert transport.calls == []


def test_transformer_runtime_uses_governed_local_chat_api() -> None:
    transport = FakeOllamaTransport()
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="請介紹你自己",
        intent="capabilities",
        model_role="daily-primary",
        output={"response": "我是星澄。", "evidence": []},
        max_tokens=256,
        temperature=0.2,
        top_k=20,
    )

    assert result["ok"] is True
    assert result["decoder"] == "quantized-transformer-autoregressive-decoder"
    assert result["parameter_class"] == "E2B"
    assert result["parameter_count"] == "2.3B effective / 5.1B total"
    assert result["remote_network_used"] is False
    assert result["third_party_foundation_weights"] is True
    chat = next(call for call in transport.calls if call[1].endswith("/api/chat"))
    assert chat[0] == "POST"
    assert chat[2]["model"] == StarTransformerRuntime.MODEL
    assert chat[2]["stream"] is False
    assert chat[2]["think"] is False
    assert chat[2]["keep_alive"] == -1
    assert chat[2]["options"]["num_ctx"] == 8_192
    assert "優先使用繁體中文" in chat[2]["messages"][0]["content"]
    assert result["residency"] == "resident"


def test_user_selected_model_uses_timeout_below_star_chat_outer_deadline() -> None:
    selected = "deepseek-r1:8b-0528-qwen3-q4_K_M"
    transport = FakeOllamaTransport(models=[{"name": selected}])
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="請分析這份較長的對話內容",
        intent="reasoning",
        model_role="user-selected-direct",
        output={"response": ""},
        requested_model=selected,
        reasoning_effort="high",
    )

    chat = next(call for call in transport.calls if call[1].endswith("/api/chat"))
    assert result["ok"] is True
    assert chat[3] == 540.0
    assert chat[3] < 600.0


def test_official_generation_defaults_are_not_overridden() -> None:
    selected = "qwen3.8:27b-q4_K_M"
    transport = FakeOllamaTransport(models=[{"name": selected}])
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="分析任務",
        intent="analysis",
        model_role="user-selected-direct",
        output={"response": "已有資料"},
        requested_model=selected,
        task_intensity="difficult",
    )

    chat = next(call for call in transport.calls if call[1].endswith("/api/chat"))
    assert result["ok"] is True
    assert set(chat[2]["options"]) == {"num_ctx", "num_predict"}
    assert chat[2]["options"]["num_predict"] == 4_096
    assert chat[2]["keep_alive"] == -1
    assert result["parameter_profile"]["context_limit"] == 32_768


def test_commander_stays_resident_and_uses_low_load_daily_profile() -> None:
    selected = "qwen3.8:27b-q4_K_M"
    transport = FakeOllamaTransport(models=[{"name": selected}])
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="整理今日工作",
        intent="general",
        model_role="resident-commander",
        output={"response": ""},
        requested_model=selected,
        task_intensity="normal",
        reasoning_effort="low",
        _release_after_generate=True,
    )

    chat = next(call for call in transport.calls if call[1].endswith("/api/chat"))
    assert result["ok"] is True
    assert runtime.RESIDENT_MODELS == {selected}
    assert runtime.KNOWN_MODEL_METADATA[selected]["residency"] == "resident"
    assert runtime.KNOWN_MODEL_METADATA[runtime.MODEL]["residency"] == "non-resident"
    assert runtime.COMMANDER_MAX_PARALLEL == 1
    assert chat[2]["keep_alive"] == -1
    assert chat[2]["options"]["num_ctx"] == 8_192
    assert chat[2]["options"]["num_predict"] == 1_536
    assert chat[2]["think"] is True


def test_model_profile_and_explicit_overrides_are_merged_at_request_time() -> None:
    selected = "qwen3-coder:30b-a3b-q4_K_M"
    transport = FakeOllamaTransport(models=[{"name": selected}])
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="修正 repository 測試",
        intent="coding",
        model_role="user-selected-direct",
        output={"coding_result": {"ok": True}},
        requested_model=selected,
        task_intensity="intermediate",
        temperature=0.25,
        max_tokens=20_000,
    )

    chat = next(call for call in transport.calls if call[1].endswith("/api/chat"))
    assert result["ok"] is True
    assert chat[2]["options"]["temperature"] == 0.25
    assert chat[2]["options"]["top_p"] == 0.8
    assert chat[2]["options"]["top_k"] == 20
    assert chat[2]["options"]["repeat_penalty"] == 1.05
    assert chat[2]["options"]["num_predict"] == 8_192
    assert result["parameter_profile"]["context_limit"] == 65_536


def test_failed_dynamic_mode_retries_once_with_immutable_base_defaults() -> None:
    selected = "qwen3-coder:30b-a3b-q4_K_M"

    class FailFirstDynamicCall(FakeOllamaTransport):
        failed = False

        def __call__(
            self,
            method: str,
            url: str,
            payload: dict[str, Any] | None,
            timeout: float,
        ) -> dict[str, Any]:
            if url.endswith("/api/chat") and not self.failed:
                self.failed = True
                self.calls.append((method, url, payload, timeout))
                raise RuntimeError("dynamic mode could not handle task")
            return super().__call__(method, url, payload, timeout)

    transport = FailFirstDynamicCall(models=[{"name": selected}])
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="困難 repository 修復",
        intent="coding",
        model_role="user-selected-direct",
        output={"coding_result": {"ok": True}},
        requested_model=selected,
        task_intensity="difficult",
    )

    chats = [call for call in transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert len(chats) == 2
    assert chats[0][2]["options"]["num_predict"] == 4_096
    assert chats[1][2]["options"]["num_predict"] == 1_024
    assert result["parameter_profile"]["source"] == "immutable-base-defaults"
    assert result["temporary_parameter_adjudication"] == {
        "advisor_model": "qwen3.8:27b-q4_K_M",
        "decision": "restore-immutable-base-defaults",
        "persisted": False,
        "attempted": True,
        "ok": True,
        "trigger_error_code": "TRANSFORMER_INFERENCE_FAILED",
    }


def test_transformer_runtime_rejects_unsupported_strict_facts() -> None:
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport("建議投入NT$999,999。"),
    )

    result = runtime.generate(
        prompt="請分析投資風險",
        intent="analysis",
        model_role="investment-specialist",
        output={"response": "目前缺少持股。", "analysis": None},
    )

    assert result["ok"] is False
    assert result["error_code"] == "TRANSFORMER_FACT_VALIDATION_FAILED"
    assert result["fallback_required"] is True


def test_transformer_runtime_lists_and_uses_an_installed_alternate_model() -> None:
    alternate = "llama3.1:8b-instruct-q4_K_M"
    transport = FakeOllamaTransport(
        models=[
            {"name": StarTransformerRuntime.MODEL, "size": 4_700_000_000},
            {"name": alternate, "size": 1_900_000_000},
        ]
    )
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    catalog = runtime.selectable_models(refresh=True)
    result = runtime.generate(
        prompt="請提供簡短範例",
        intent="capabilities",
        model_role="daily-primary",
        output={"response": "簡短範例"},
        requested_model=alternate,
    )

    assert [item["name"] for item in catalog] == [StarTransformerRuntime.MODEL, alternate]
    assert catalog[0]["default"] is True
    assert result["ok"] is True
    assert result["model"] == alternate
    assert result["parameter_count"] == "8.03B"
    assert result["model_selected_by_user"] is True
    chat = next(call for call in transport.calls if call[1].endswith("/api/chat"))
    assert chat[2]["model"] == alternate
    assert chat[2]["keep_alive"] == 0
    assert result["residency"] == "non-resident"


def test_embedding_model_is_isolated_from_chat_selection() -> None:
    embedding = StarTransformerRuntime.EMBEDDING_MODEL
    transport = FakeOllamaTransport(
        models=[
            {"name": StarTransformerRuntime.MODEL},
            {"name": embedding},
        ]
    )
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    catalog = runtime.selectable_models(refresh=True)
    vectors = runtime.embed(["查詢", "文件"])

    assert embedding not in {item["name"] for item in catalog}
    assert runtime.status()["embedding_model_installed"] is True
    assert len(vectors) == 2
    assert all(len(vector) == 2 for vector in vectors)
    embed_call = next(call for call in transport.calls if call[1].endswith("/api/embed"))
    assert embed_call[2]["keep_alive"] == -1


def test_lightweight_and_fast_coding_models_have_governed_roles() -> None:
    models = [
        {"name": StarTransformerRuntime.MODEL},
        {"name": "nemotron-3-nano:4b"},
        {"name": "qwen3.5:9b-q4_K_M"},
        {"name": "qwen2.5-coder:7b"},
        {"name": "qwen3.6:35b-a3b-coding"},
    ]
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(models=models),
    )

    catalog = {
        item["name"]: item for item in runtime.selectable_models(refresh=True)
    }

    assert catalog["nemotron-3-nano:4b"]["usage_class"] == (
        "lightweight-tool-reasoning-and-fast-fallback"
    )
    assert catalog["nemotron-3-nano:4b"]["context_window"] == 262_144
    assert catalog["qwen2.5-coder:7b"]["usage_class"] == (
        "fast-coding-and-command-execution-fallback"
    )
    assert catalog["qwen2.5-coder:7b"]["context_window"] == 32_768
    assert runtime.preferred_model_for_intent("command_understanding", "low") == (
        "qwen3.5:9b-q4_K_M"
    )
    assert runtime.preferred_model_for_intent("coding", "low") == (
        "qwen2.5-coder:7b"
    )
    assert runtime.preferred_model_for_intent("coding", "medium") == (
        "qwen3.6:35b-a3b-coding"
    )


def test_transformer_runtime_rejects_uninstalled_model_without_inference() -> None:
    transport = FakeOllamaTransport()
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="test",
        intent="capabilities",
        model_role="daily-primary",
        output={},
        requested_model="llama4:scout",
    )

    assert result["ok"] is False
    assert result["error_code"] == "TRANSFORMER_MODEL_NOT_INSTALLED"
    assert not any(call[1].endswith("/api/chat") for call in transport.calls)


def test_automatic_search_routing_uses_installed_gemma() -> None:
    gemma = "gemma4:e2b-it-qat"
    transport = FakeOllamaTransport(
        models=[{"name": StarTransformerRuntime.MODEL}, {"name": gemma}]
    )
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="搜尋資料",
        intent="search",
        model_role="daily-primary",
        output={"response": "搜尋結果"},
    )

    assert result["ok"] is True
    assert result["model"] == gemma
    assert result["model_selected_by_user"] is False


def test_automatic_math_routing_starts_gpt_oss_with_safe_context() -> None:
    math_model = "gpt-oss:20b"
    transport = FakeOllamaTransport(
        models=[{"name": StarTransformerRuntime.MODEL}, {"name": math_model}]
    )
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="計算 1 加 1",
        intent="calculation",
        model_role="mathematics-specialist",
        output={"response": "2"},
        reasoning_effort="high",
    )

    assert result["ok"] is True
    assert result["model"] == math_model
    assert result["context_window"] == 8_192
    assert result["reasoning_effort"] == "high"
    chat = next(call for call in transport.calls if call[1].endswith("/api/chat"))
    assert chat[2]["options"]["num_ctx"] == 8_192
    assert chat[2]["think"] == "high"


def test_memory_pressure_retries_same_content_with_lower_context() -> None:
    selected = "qwen3:30b-a3b-instruct-2507-q4_K_M"

    class MemoryBoundTransport(FakeOllamaTransport):
        def __call__(
            self,
            method: str,
            url: str,
            payload: dict[str, Any] | None,
            timeout: float,
        ) -> dict[str, Any]:
            if url.endswith("/api/chat") and isinstance(payload, dict):
                self.calls.append((method, url, payload, timeout))
                if int(payload["options"]["num_ctx"]) > 4_096:
                    raise RuntimeError("CUDA out of memory")
                return {
                    "message": {"role": "assistant", "content": "完整內容已完成"},
                    "prompt_eval_count": 120,
                    "eval_count": 18,
                }
            return super().__call__(method, url, payload, timeout)

    transport = MemoryBoundTransport(models=[{"name": selected}])
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="不可遺失的完整原始內容",
        intent="capabilities",
        model_role="user-selected-direct",
        output={"response": "完整受治理內容"},
        requested_model=selected,
    )

    chats = [call for call in transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert [call[2]["options"]["num_ctx"] for call in chats] == [8_192, 4_096]
    assert chats[0][2]["messages"] == chats[1][2]["messages"]
    assert result["adaptive_context"]["memory_pressure_recovered"] is True
    assert result["adaptive_context"]["content_preserved"] is True


def test_stable_model_can_raise_context_for_larger_input() -> None:
    selected = "gpt-oss:20b"
    transport = FakeOllamaTransport(models=[{"name": selected}])
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    for _ in range(runtime.CONTEXT_GROWTH_SUCCESS_THRESHOLD):
        result = runtime.generate(
            prompt="短內容",
            intent="calculation",
            model_role="mathematics-specialist",
            output={"response": "完成"},
            requested_model=selected,
        )
        assert result["ok"] is True

    result = runtime.generate(
        prompt="長內容" * 4_500,
        intent="calculation",
        model_role="mathematics-specialist",
        output={"response": "完成"},
        requested_model=selected,
    )

    chats = [call for call in transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert chats[-1][2]["options"]["num_ctx"] == 16_384
    assert result["adaptive_context"]["required_context"] == 16_384


def test_pipeline_resumes_from_durable_stage_checkpoint(tmp_path: Path) -> None:
    first = "qwen3.5:9b-q4_K_M"
    final = "gemma4:e2b-it-qat"

    class ResumeTransport(FakeOllamaTransport):
        fail_final = True

        def __call__(
            self,
            method: str,
            url: str,
            payload: dict[str, Any] | None,
            timeout: float,
        ) -> dict[str, Any]:
            if (
                url.endswith("/api/chat")
                and isinstance(payload, dict)
                and payload.get("model") == final
                and self.fail_final
            ):
                self.calls.append((method, url, payload, timeout))
                raise RuntimeError("temporary model failure")
            return super().__call__(method, url, payload, timeout)

    transport = ResumeTransport(models=[{"name": first}, {"name": final}])
    runtime = StarTransformerRuntime(
        enabled=True, transport=transport, checkpoint_root=tmp_path
    )
    request = {
        "prompt": "需完整續跑的搜尋流程",
        "intent": "search",
        "model_role": "daily-primary",
        "output": {"response": "原始內容"},
        "division_pipeline": True,
    }

    failed = runtime.generate(**request)
    transport.fail_final = False
    resumed = runtime.generate(**request)

    first_model_calls = [
        call
        for call in transport.calls
        if call[1].endswith("/api/chat") and call[2]["model"] == first
    ]
    assert failed["ok"] is False
    assert failed["division_pipeline"]["checkpoint_persisted"] is True
    assert resumed["ok"] is True
    assert resumed["division_pipeline"]["checkpoint_resumed"] is True
    assert resumed["division_pipeline"]["stages"][0]["checkpoint_restored"] is True
    assert len(first_model_calls) == 1

    database = tmp_path / "runtime" / "state" / "transformer-runtime-checkpoints.sqlite3"
    with sqlite3.connect(database) as connection:
        active_runs = connection.execute(
            "SELECT COUNT(*) FROM transformer_pipeline_run"
        ).fetchone()[0]
    assert active_runs == 0


def test_no_reasoning_uses_gemma_fast_path() -> None:
    transport = FakeOllamaTransport(
        models=[
            {"name": "gemma4:e2b-it-qat"},
            {"name": "gpt-oss:20b"},
        ]
    )
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="快速回答",
        intent="reasoning",
        model_role="daily-primary",
        output={"response": "快速回答"},
        reasoning_effort="none",
    )

    chat = next(call for call in transport.calls if call[1].endswith("/api/chat"))
    assert result["ok"] is True
    assert result["model"] == "gemma4:e2b-it-qat"
    assert result["reasoning_effort"] == "none"
    assert chat[2]["think"] is False


@pytest.mark.parametrize(
    ("effort", "expected_models"),
    [
        (
            "low",
            [
                "qwen3.5:9b-q4_K_M",
                "qwen3:30b-a3b-instruct-2507-q4_K_M",
                "qwen3.8:27b-q4_K_M",
                "gpt-oss:20b",
                "qwen3.6:35b-a3b-coding",
                "qwen3.8:27b-q4_K_M",
                "gemma4:e2b-it-qat",
            ],
        ),
        (
            "high",
            [
                "qwen3.5:9b-q4_K_M",
                "qwen3:30b-a3b-instruct-2507-q4_K_M",
                "qwen3.8:27b-q4_K_M",
                "gpt-oss:20b",
                "qwen3.6:35b-a3b-coding",
                "qwen3.8:27b-q4_K_M",
                "gemma4:e2b-it-qat",
            ],
        ),
    ],
)
def test_complex_pipeline_follows_the_full_automatic_sequence(
    effort: str, expected_models: list[str]
) -> None:
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(
            models=[{"name": name} for name in expected_models]
        ),
    )

    result = runtime.generate(
        prompt="處理複雜任務",
        intent="capabilities",
        model_role="daily-primary",
        output={"response": "已建立受治理的任務背景"},
        reasoning_effort=effort,
        complex_pipeline=True,
    )

    calls = [call for call in runtime._transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert result["complex_pipeline"]["executed"] is True
    assert [call[2]["model"] for call in calls] == expected_models
    assert [call[2]["keep_alive"] for call in calls] == [0, 0, 0, 0, 0, 0, -1]


def test_complex_pipeline_uses_single_stage_backup_after_primary_failure() -> None:
    primary = "qwen3.5:9b-q4_K_M"
    backup = "nemotron-3-nano:4b"
    models = [
        primary,
        backup,
        "qwen3:30b-a3b-instruct-2507-q4_K_M",
        "qwen3.8:27b-q4_K_M",
        "gpt-oss:20b",
        "qwen3.6:35b-a3b-coding",
        "gemma4:e2b-it-qat",
    ]

    class FailPrimaryOnceTransport(FakeOllamaTransport):
        failed = False

        def __call__(
            self,
            method: str,
            url: str,
            payload: dict[str, Any] | None,
            timeout: float,
        ) -> dict[str, Any]:
            if (
                url.endswith("/api/chat")
                and isinstance(payload, dict)
                and payload.get("model") == primary
                and not self.failed
            ):
                self.failed = True
                self.calls.append((method, url, payload, timeout))
                raise RuntimeError("simulated primary inference failure")
            return super().__call__(method, url, payload, timeout)

    transport = FailPrimaryOnceTransport(
        models=[{"name": name} for name in models]
    )
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="自動接替命令解析",
        intent="capabilities",
        model_role="daily-primary",
        output={"response": "已建立受治理的任務背景"},
        reasoning_effort="medium",
        complex_pipeline=True,
    )

    first_stage = result["complex_pipeline"]["stages"][0]
    calls = [call for call in transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert [call[2]["model"] for call in calls[:2]] == [primary, backup]
    assert first_stage["primary_model"] == primary
    assert first_stage["backup_model"] == backup
    assert first_stage["model"] == backup
    assert first_stage["backup_used"] is True
    assert [attempt["ok"] for attempt in first_stage["attempts"]] == [False, True]


def test_high_complex_reasoning_adds_domain_reviewers_before_final_verifier() -> None:
    expected_models = [
        "qwen3.5:9b-q4_K_M",
        "qwen3:30b-a3b-instruct-2507-q4_K_M",
        "deepseek-r1:8b-0528-qwen3-q4_K_M",
        "gpt-oss:20b",
        "qwen3.6:35b-a3b-coding",
        "qwen3.8:27b-q4_K_M",
        "gemma4:e2b-it-qat",
    ]
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(
            models=[{"name": name} for name in expected_models]
        ),
    )

    result = runtime.generate(
        prompt="執行多階段風險推理",
        intent="risk",
        model_role="investment-specialist",
        output={"response": "已提供受治理資料"},
        reasoning_effort="high",
        complex_pipeline=True,
    )

    calls = [call for call in runtime._transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert [call[2]["model"] for call in calls] == expected_models
    assert result["complex_pipeline"]["maximum_concurrent_transformers"] == 1
    assert result["complex_pipeline"]["resource_policy"] == (
        "single-model-exclusive-load-then-release-and-handoff"
    )
    assert [stage["stage"] for stage in result["complex_pipeline"]["stages"]] == [
        "understand-command",
        "allocate-tasks",
        "perform-domain-reasoning",
        "integrate-ordered-work",
        "execute-integrated-plan",
        "inspect-execution",
        "produce-traditional-chinese-result",
    ]


@pytest.mark.parametrize(
    ("effort", "expected_models"),
    [
        (
            "medium",
            [
                "deepseek-r1:8b-0528-qwen3-q4_K_M",
                "gpt-oss:20b",
            ],
        ),
        (
            "high",
            [
                "deepseek-r1:8b-0528-qwen3-q4_K_M",
                "qwen3.8:27b-q4_K_M",
                "gpt-oss:20b",
            ],
        ),
    ],
)
def test_reasoning_pipeline_uses_deepseek_then_optional_qwen38_before_gpt(
    effort: str, expected_models: list[str]
) -> None:
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(
            models=[{"name": name} for name in expected_models]
        ),
    )

    result = runtime.generate(
        prompt="分析風險並統籌結論",
        intent="risk",
        model_role="investment-specialist",
        output={"response": "已提供受治理資料"},
        reasoning_effort=effort,
        reasoning_pipeline=True,
    )

    calls = [call for call in runtime._transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert result["reasoning_pipeline"]["executed"] is True
    assert [call[2]["model"] for call in calls] == expected_models
    assert all(call[2]["keep_alive"] == 0 for call in calls)


def test_search_division_pipeline_hands_off_then_keeps_final_gemma_resident() -> None:
    models = ["qwen3.5:9b-q4_K_M", "gemma4:e2b-it-qat"]
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(
            models=[{"name": name} for name in models]
        ),
    )

    result = runtime.generate(
        prompt="搜尋並整理資料",
        intent="search",
        model_role="daily-primary",
        output={"response": "已取得受治理資料"},
        reasoning_effort="medium",
        division_pipeline=True,
    )

    calls = [call for call in runtime._transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert [call[2]["model"] for call in calls] == models
    assert [call[2]["keep_alive"] for call in calls] == [0, -1]
    assert calls[0][2]["think"] is False
    assert [
        stage["released_after_stage"]
        for stage in result["division_pipeline"]["stages"]
    ] == [True, False]


def test_first_stage_uses_command_understanding_backup_when_primary_is_absent() -> None:
    models = ["nemotron-3-nano:4b", "gemma4:e2b-it-qat"]
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(
            models=[{"name": name} for name in models]
        ),
    )

    result = runtime.generate(
        prompt="整理這份命令",
        intent="command_understanding",
        model_role="daily-primary",
        output={"response": "已理解"},
        reasoning_effort="low",
        division_pipeline=True,
    )

    calls = [call for call in runtime._transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert [call[2]["model"] for call in calls] == models
    assert "優先使用繁體中文" in calls[0][2]["messages"][0]["content"]


def test_simple_task_intensity_prefers_existing_fast_model_without_reassigning_roles() -> None:
    fast = "qwen2.5-coder:7b"
    specialist = "qwen3.6:35b-a3b-coding"
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(
            models=[{"name": fast}, {"name": specialist}]
        ),
    )

    result = runtime.generate(
        prompt="修正一行程式",
        intent="coding",
        model_role="coding-specialist",
        output={"coding_result": {"ok": True}},
        reasoning_effort="high",
        task_intensity="simple",
    )

    chat = next(
        call for call in runtime._transport.calls if call[1].endswith("/api/chat")
    )
    assert result["ok"] is True
    assert result["model"] == fast
    assert result["task_intensity"] == "simple"
    assert chat[2]["model"] == fast


def test_coding_division_pipeline_uses_interpreter_executor_and_high_verifier() -> None:
    expected_models = [
        "qwen3.5:9b-q4_K_M",
        "qwen3.6:35b-a3b-coding",
        "gpt-oss:20b",
    ]
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(
            models=[{"name": name} for name in expected_models]
        ),
    )

    result = runtime.generate(
        prompt="修正程式並驗證",
        intent="coding",
        model_role="coding-specialist",
        output={"coding_result": {"ok": True}},
        reasoning_effort="high",
        division_pipeline=True,
    )

    calls = [call for call in runtime._transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert [call[2]["model"] for call in calls] == expected_models
    assert all(call[2]["keep_alive"] == 0 for call in calls)


def test_automatic_route_falls_back_to_resident_model_after_specialist_failure() -> None:
    class FailingSpecialistTransport(FakeOllamaTransport):
        def __call__(
            self,
            method: str,
            url: str,
            payload: dict[str, Any] | None,
            timeout: float,
        ) -> dict[str, Any]:
            if (
                url.endswith("/api/chat")
                and isinstance(payload, dict)
                and payload.get("model") == "qwen3.5:9b-q4_K_M"
            ):
                self.calls.append((method, url, payload, timeout))
                raise OSError("specialist unavailable")
            return super().__call__(method, url, payload, timeout)

    transport = FailingSpecialistTransport(
        response_text="已由常駐模型接手。",
        models=[
            {"name": "qwen3.5:9b-q4_K_M"},
            {"name": "gemma4:e2b-it-qat"},
        ],
    )
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="整理一般資料",
        intent="data",
        model_role="daily-primary",
        output={"response": "一般資料"},
        reasoning_effort="medium",
    )

    calls = [call for call in transport.calls if call[1].endswith("/api/chat")]
    assert result["ok"] is True
    assert result["model"] == "gemma4:e2b-it-qat"
    assert result["model_selected_by_user"] is False
    assert result["automatic_model_route"]["fallback_used"] is True
    assert [attempt["model"] for attempt in result["automatic_model_route"]["attempts"]] == [
        "qwen3.5:9b-q4_K_M",
        "gemma4:e2b-it-qat",
    ]
    assert [call[2]["model"] for call in calls] == [
        "qwen3.5:9b-q4_K_M",
        "gemma4:e2b-it-qat",
    ]
    assert all(call[3] == 180.0 for call in calls)


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        ("conversation", "gemma4:e2b-it-qat"),
        ("reading", "gemma4:e2b-it-qat"),
        ("coding", "qwen3.6:35b-a3b-coding"),
        ("reasoning", "deepseek-r1:8b-0528-qwen3-q4_K_M"),
        ("analysis", "deepseek-r1:8b-0528-qwen3-q4_K_M"),
        ("search", "qwen3.5:9b-q4_K_M"),
        ("data_organization", "qwen3.5:9b-q4_K_M"),
        ("self_upgrade", "qwen3.6:35b-a3b-coding"),
    ],
)
def test_automatic_model_classification(intent: str, expected: str) -> None:
    model_names = {
        StarTransformerRuntime.MODEL,
        "gemma4:e2b-it-qat",
        "gpt-oss:20b",
        "qwen3:30b-a3b-instruct-2507-q4_K_M",
        "deepseek-r1:8b-0528-qwen3-q4_K_M",
        "qwen3.5:9b-q4_K_M",
        "llama3.1:8b-instruct-q4_K_M",
        "qwen3.8:27b-q4_K_M",
        "qwen3.6:35b-a3b-coding",
    }
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(
            models=[{"name": name} for name in sorted(model_names)]
        ),
    )

    runtime.probe(refresh=True)

    assert runtime.preferred_model_for_intent(intent) == expected


def test_daily_models_are_grouped_separately_from_specialists_and_fallback() -> None:
    names = [
        "gemma4:e2b-it-qat",
        "gpt-oss:20b",
        "qwen3:30b-a3b-instruct-2507-q4_K_M",
        "deepseek-r1:8b-0528-qwen3-q4_K_M",
        "qwen3.5:9b-q4_K_M",
        "qwen3.8:27b-q4_K_M",
        "qwen3.6:35b-a3b-coding",
        "llama3.1:8b-instruct-q4_K_M",
    ]
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(models=[{"name": name} for name in names]),
    )

    catalog = {item["name"]: item for item in runtime.selectable_models(refresh=True)}

    assert catalog["gemma4:e2b-it-qat"]["daily_group"] == "fast"
    assert catalog["gemma4:e2b-it-qat"]["residency"] == "resident"
    assert catalog["gemma4:e2b-it-qat"]["quantization"] == "QAT-4bit"
    assert catalog["gemma4:e2b-it-qat"]["context_window"] == 131_072
    assert catalog["gpt-oss:20b"]["quantization"] == "MXFP4"
    assert catalog["gpt-oss:20b"]["context_window"] == 131_072
    assert catalog["qwen3:30b-a3b-instruct-2507-q4_K_M"]["quantization"] == "Q4_K_M"
    assert catalog["qwen3:30b-a3b-instruct-2507-q4_K_M"]["context_window"] == 262_144
    assert catalog["deepseek-r1:8b-0528-qwen3-q4_K_M"]["quantization"] == "Q4_K_M"
    assert catalog["deepseek-r1:8b-0528-qwen3-q4_K_M"]["context_window"] == 131_072
    assert catalog["qwen3.5:9b-q4_K_M"]["usage_class"] == "search-and-tool-coordinator"
    assert catalog["qwen3.5:9b-q4_K_M"]["context_window"] == 262_144
    assert catalog["llama3.1:8b-instruct-q4_K_M"]["usage_class"] == (
        "lightweight-fallback-and-manual"
    )
    assert catalog["qwen3.8:27b-q4_K_M"]["evaluation"] == {
        "speed": 1,
        "strength": 5,
        "reasoning_depth": 5,
    }
    assert catalog["qwen3.8:27b-q4_K_M"]["usage_class"] == "advanced-reasoning"
    assert catalog["gpt-oss:20b"]["usage_class"] == "integration-coordinator"
    assert catalog["deepseek-r1:8b-0528-qwen3-q4_K_M"]["usage_class"] == "medium-reasoning"
    assert catalog["qwen3:30b-a3b-instruct-2507-q4_K_M"]["usage_class"] == "complex"
    assert catalog["qwen3:30b-a3b-instruct-2507-q4_K_M"]["residency"] == "non-resident"


def test_runtime_reports_memory_bounded_residency_policy() -> None:
    runtime = StarTransformerRuntime(enabled=False)

    policy = runtime.status()["residency_policy"]

    assert policy["resident"] == ["gemma4:e2b-it-qat"]
    assert policy["unknown_installed_models"] == "non-resident"
    assert policy["resident_evicted_before_non_resident"] is True
    assert policy["pipeline_release_after_each_stage"] is True
    assert policy["maximum_concurrent_transformers"] == 1


def test_llama_is_the_lightweight_daily_fallback_when_gemma_is_absent() -> None:
    llama = "llama3.1:8b-instruct-q4_K_M"
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(models=[{"name": llama}]),
    )

    runtime.probe(refresh=True)

    assert runtime.preferred_model_for_intent("conversation") == llama
    assert runtime.preferred_model_for_intent("reading") == llama
    assert runtime.preferred_model_for_intent("search") == llama
    assert runtime.preferred_model_for_intent("training") == llama


def test_service_denies_direct_runtime_model_selection(tmp_path: Path) -> None:
    service = LocalAiService(
        tmp_path,
        transformer_runtime=StarTransformerRuntime(
            enabled=True, transport=FakeOllamaTransport()
        ),
    )

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {"prompt": "test", "runtime_model": StarTransformerRuntime.MODEL},
        )
    )

    assert result["ok"] is False
    assert result["error_code"] == "RUNTIME_MODEL_SELECTION_DENIED"


def test_chat_and_coding_use_governed_workspace_without_programming_folder(
    tmp_path: Path,
) -> None:
    service = object.__new__(LocalAiService)
    service.tool_root = tmp_path / "local-model"

    assert service._programming_folder_for_request(
        {"conversation_mode": "chat"}
    ) == str(tmp_path)
    assert service._programming_folder_for_request(
        {"conversation_mode": "coding"}
    ) == str(tmp_path)
    assert service._programming_folder_for_request(
        {"conversation_mode": "chat", "programming_folder": "C:/chosen"}
    ) == "C:/chosen"


def test_service_accepts_governed_star_chat_model_selection(tmp_path: Path) -> None:
    selected = "llama3.1:8b-instruct-q4_K_M"
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport(
            models=[{"name": StarTransformerRuntime.MODEL}, {"name": selected}]
        ),
    )
    service = LocalAiService(tmp_path, transformer_runtime=runtime)

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請介紹你自己",
                "runtime_model": selected,
                "_runtime_model_selection_authorized": True,
            },
        )
    )

    assert result["mode"] == "governed-local-transformer-llm"
    assert result["generation"]["model"] == selected
    assert result["generation"]["parameter_count"] == "8.03B"


def test_service_accepts_governed_star_native_model_selection(tmp_path: Path) -> None:
    service = LocalAiService(
        tmp_path,
        transformer_runtime=StarTransformerRuntime(enabled=False),
    )

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "Hello Star",
                "runtime_model": "star-main-native-model",
                "_runtime_model_selection_authorized": True,
            },
        )
    )

    assert result["ok"] is True
    assert result["model"] == "star-main-native-model"
    assert result["model_name"] == "星澄"
    assert result["selected_runtime_model"] == "star-main-native-model"
    assert result["model_selection"] == "user-selected"
    assert result["manual_model_selection"] is True
    assert result["native_database_access"]["enabled"] is True
    assert result["native_database_access"]["database_scope"] == (
        "all-project-databases-excluding-governance-rule"
    )
    assert result["native_database_access"]["default_operational_database"] == "main"
    assert result["native_database_access"]["investment_database_access"] is True
    assert "transformer_inference" not in result


def test_selected_star_defaults_to_main_database_with_project_wide_permission(
    tmp_path: Path,
) -> None:
    service = LocalAiService(
        tmp_path,
        transformer_runtime=StarTransformerRuntime(enabled=False),
    )
    main = service.repositories[service.models.MAIN.model_id]
    investment = service.repositories[service.models.INVESTMENT.model_id]
    main_memory = main.store_brokered_memory(
        memory_id="star-own-memory",
        kind="user-context",
        title="海岳計畫",
        content="海岳計畫使用藍色標籤。",
        business_scope="general",
        source_type="user-approved",
        source_id="test-main",
        source_model_id=service.NATIVE_MODEL_ID,
        broker_model_id=service.NATIVE_MODEL_ID,
        confidence=1.0,
    )
    investment.store_brokered_memory(
        memory_id="investment-private-memory",
        kind="investment-context",
        title="海岳計畫",
        content="這筆內容只屬於投資資料庫。",
        business_scope="general",
        source_type="user-approved",
        source_id="test-investment",
        source_model_id=service.models.INVESTMENT.model_id,
        broker_model_id=service.NATIVE_MODEL_ID,
        confidence=1.0,
    )
    investment_records_before = investment.database_status()["tables"]["inference_log"]

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請說明海岳計畫",
                "runtime_model": service.NATIVE_MODEL_ID,
                "_runtime_model_selection_authorized": True,
            },
        )
    )

    assert result["ok"] is True
    assert result["native_database_access"]["specialist_database_access"] is True
    assert result["context_retrieval"]["memory_ids"] == [main_memory["memory_id"]]
    assert result["memory_interoperability"]["stored_count"] == 0
    assert result["memory_interoperability"]["persistence_requested"] is False
    assert investment.database_status()["tables"]["inference_log"] == (
        investment_records_before
    )

    _, saved = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請記住海岳計畫使用藍色標籤",
                "runtime_model": service.NATIVE_MODEL_ID,
                "_runtime_model_selection_authorized": True,
            },
        )
    )

    assert saved["memory_interoperability"]["stored_count"] == 1
    assert saved["memory_interoperability"]["persistence_requested"] is True
    assert saved["memory_interoperability"]["platform_validated"] is True


def test_selected_ollama_model_never_receives_or_writes_star_private_content(
    tmp_path: Path,
) -> None:
    selected = "llama3.1:8b-instruct-q4_K_M"
    transport = FakeOllamaTransport(
        models=[{"name": StarTransformerRuntime.MODEL}, {"name": selected}]
    )
    service = LocalAiService(
        tmp_path,
        transformer_runtime=StarTransformerRuntime(enabled=True, transport=transport),
    )
    main = service.repositories[service.models.MAIN.model_id]
    main.store_brokered_memory(
        memory_id="star-private-secret",
        kind="user-context",
        title="私有內容",
        content="星澄私有識別詞 NIGHT-ORCHID-731",
        business_scope="general",
        source_type="user-approved",
        source_id="test-main",
        source_model_id=service.NATIVE_MODEL_ID,
        broker_model_id=service.NATIVE_MODEL_ID,
        confidence=1.0,
    )
    inference_count = main.database_status()["tables"]["inference_log"]

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請介紹你自己",
                "runtime_model": selected,
                "_runtime_model_selection_authorized": True,
            },
        )
    )

    chat = next(call for call in transport.calls if call[1].endswith("/api/chat"))
    assert "NIGHT-ORCHID-731" not in json.dumps(chat[2], ensure_ascii=False)
    assert "native_private_context" not in json.dumps(chat[2], ensure_ascii=False)
    assert main.database_status()["tables"]["inference_log"] == inference_count
    assert result["memory_interoperability"]["stored_count"] == 0


def test_selected_star_opens_training_capability_and_operation_records(
    tmp_path: Path,
) -> None:
    service = LocalAiService(
        tmp_path,
        transformer_runtime=StarTransformerRuntime(enabled=False),
    )
    main = service.repositories[service.models.MAIN.model_id]
    main.store_language_training_example(
        intent="conversation",
        input_text="測試輸入",
        target_text="測試輸出",
        source_type="user-approved",
        quality_score=0.9,
        validation={"facts_preserved": True},
    )
    main.store_capability_composition(
        {
            "composition_id": "native-capability-1",
            "status": "approved",
            "implementation_target": "xingcheng",
        }
    )
    main.record(service.NATIVE_MODEL_ID, {"prompt": "先前操作"}, {"ok": True})

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "檢查自己的資料庫",
                "runtime_model": service.NATIVE_MODEL_ID,
                "_runtime_model_selection_authorized": True,
            },
        )
    )

    counts = result["context_retrieval"]["native_private_record_counts"]
    assert result["context_retrieval"]["native_private_database_opened"] is True
    assert counts["training_examples"] == 1
    assert counts["capability_compositions"] == 1
    assert counts["operation_records"] == 1
    assert result["native_database_access"]["investment_database_access"] is True
    assert result["native_database_access"]["ollama_model_database_access"] is True


def test_service_promotes_transformer_output_without_training_it(
    tmp_path: Path,
) -> None:
    runtime = StarTransformerRuntime(
        enabled=True,
        transport=FakeOllamaTransport("我是星澄，現在使用本機 Transformer 產生回答。"),
    )
    service = LocalAiService(tmp_path, transformer_runtime=runtime)

    _, result = asyncio.run(
        service.handle("xingcheng_infer", {"prompt": "請介紹你自己"})
    )

    assert result["mode"] == "governed-local-transformer-llm"
    assert result["response"] == "我是星澄，現在使用本機 Transformer 產生回答。"
    assert result["generation"]["model_type"] == "quantized-local-decoder-transformer"
    assert result["external_model_used"] is True
    assert result["remote_model_used"] is False
    assert result["external_ai_used"] is False
    assert result["star_native_model_used"] is False
    assert result["model"] == StarTransformerRuntime.MODEL
    assert result["coordinator_model"] == "gpt-oss:20b"
    assert result["self_training"]["accepted"] is False
    assert service.runtime_health()["runtime_metrics"]["transformer_success_count"] == 1


def test_service_maps_task_intensity_to_reasoning_paths_without_role_reassignment(
    tmp_path: Path,
) -> None:
    class CapturingRuntime(StarTransformerRuntime):
        def __init__(self) -> None:
            super().__init__(enabled=True, transport=FakeOllamaTransport())
            self.generation_calls: list[dict[str, Any]] = []

        def generate(self, **kwargs: Any) -> dict[str, Any]:
            self.generation_calls.append(dict(kwargs))
            return {
                "ok": True,
                "text": "已完成",
                "decoder": "quantized-transformer-autoregressive-decoder",
                "model": self.MODEL,
                "model_family": self.MODEL_FAMILY,
                "parameter_class": self.PARAMETER_CLASS,
                "parameter_count": self.PARAMETER_COUNT,
                "quantization": self.QUANTIZATION,
                "context_window": 8_192,
                "facts_supported": True,
                "eval_count": 2,
                "foundation_model_license": "Apache-2.0",
            }

    runtime = CapturingRuntime()
    service = LocalAiService(tmp_path, transformer_runtime=runtime)

    asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請介紹你自己",
                "reasoning_effort": "medium",
                "task_intensity": "simple",
            },
        )
    )
    asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請介紹你自己",
                "reasoning_effort": "medium",
                "task_intensity": "difficult",
            },
        )
    )

    simple, difficult = runtime.generation_calls
    assert simple["task_intensity"] == "simple"
    assert simple["complex_pipeline"] is False
    assert simple["reasoning_pipeline"] is False
    assert simple["division_pipeline"] is False
    assert difficult["task_intensity"] == "difficult"
    assert difficult["complex_pipeline"] is True
    assert difficult["division_pipeline"] is False
    assert runtime.COMMAND_UNDERSTANDING_PRIMARY_MODEL == "qwen3.5:9b-q4_K_M"
    assert runtime.COMMAND_UNDERSTANDING_BACKUP_MODEL == "nemotron-3-nano:4b"


def test_service_status_reports_the_governed_multi_model_architecture(
    tmp_path: Path,
) -> None:
    service = LocalAiService(tmp_path)

    _, status = asyncio.run(service.handle("xingcheng_status", {}))

    assert status["platform_mode"] == "context-aware-multitask-model-platform"
    assert status["entry_gateway"] == "all-ai-business-entries"
    assert status["platform_permission_profile"] == "local-model-platform-v1"
    assert {item["id"] for item in status["platform_labels"]} == {
        "ai-entry-gateway",
        "context-multitask",
        "local-model-routing",
        "selected-model-direct",
        "ollama-loopback",
        "governance-controlled",
    }
    assert status["star_native_model_permissions"]["entry_dispatch"] is False
    assert status["star_native_model_permissions"]["database_read"] is True
    assert status["star_native_model_permissions"]["database_write"] is True
    assert status["star_native_model_permissions"]["investment_database_write"] is True
    assert status["platform_concurrency"] == "asynchronous-service-isolated"
    assert status["autonomous_agent"]["enabled"] is True
    assert status["autonomous_agent"]["star_native_model_included"] is False
    assert status["autonomous_agent"]["understanding_authority"]["primary"] == (
        "qwen3.5:9b-q4_K_M"
    )
    assert status["autonomous_agent"]["understanding_authority"]["backup"] == (
        "nemotron-3-nano:4b"
    )
    assert status["autonomous_agent"]["project_scope"] == (
        "all-project-source-excluding-governance-rule"
    )
    assert status["platform_services"]["model-dialogue-manual"] == (
        "selected-model-direct-under-governance"
    )
    assert status["platform_services"]["investment-manager"].startswith("automatic-")
    assert status["main_system_companion_tools"] == ["star-chat"]
    assert status["model_dialogue"] == {
        "tool_id": "star-chat",
        "independent_only_in": "main-system",
        "physical_owner_root": "local-model",
        "settings_owner": "xingcheng",
        "business_layer_owner": "xingcheng",
        "permission_profile": "local-model-platform-v1",
        "cache_owner": "xingcheng",
        "cache_storage": "local-model/runtime/cache/companions/star-chat",
        "backup_owner": "xingcheng",
        "backup_storage": "global-cleaner/data/business/backups/xingcheng",
        "separate_model_service": False,
        "separate_settings_layer": False,
        "separate_business_layer": False,
    }
    assert status["model_architecture"] == (
        "governed-local-multi-model-transformer+deterministic-specialists+"
        "statistical-safety-fallback"
    )
    assert status["orchestration"]["integration_model"] == "gpt-oss:20b"
    assert status["orchestration"]["integration_backup"] == (
        "qwen3:30b-a3b-instruct-2507-q4_K_M"
    )
    assert status["orchestration"]["external_ai_used"] is False
    assert status["orchestration"]["star_native_model_included"] is False
    assert status["orchestration"]["result_model"] == "gemma4:e2b-it-qat"
    assert status["orchestration"]["task_allocation_model"] == (
        "qwen3:30b-a3b-instruct-2507-q4_K_M"
    )
    assert status["orchestration"]["execution_model"] == (
        "qwen3.6:35b-a3b-coding"
    )
    assert status["orchestration"]["majority_vote_models"] == []
    assert status["orchestration"]["capability_composition_owner"] == (
        "star-main-native-model"
    )
    assert status["investment_model_roles"]["selection_mode"] == "automatic-only"
    assert status["investment_model_roles"]["manual_model_override"] is False
    assert status["self_training"]["training_coordinator_model"] == (
        "gemma4:e2b-it-qat"
    )


def test_service_does_not_fall_back_to_star_when_ollama_is_unavailable(
    tmp_path: Path,
) -> None:
    def unavailable(
        method: str,
        url: str,
        payload: dict[str, Any] | None,
        timeout: float,
    ) -> dict[str, Any]:
        raise OSError("offline")

    service = LocalAiService(
        tmp_path,
        transformer_runtime=StarTransformerRuntime(enabled=True, transport=unavailable),
    )

    _, result = asyncio.run(
        service.handle("xingcheng_infer", {"prompt": "請介紹你自己"})
    )

    assert result["ok"] is False
    assert result["error_code"] == "TRANSFORMER_RUNTIME_UNAVAILABLE"
    assert result["star_native_model_used"] is False
    assert result["fallback_model_used"] is False
    assert service.runtime_health()["runtime_metrics"]["transformer_fallback_count"] == 1


def test_selected_transformer_failure_never_falls_back_to_star(
    tmp_path: Path,
) -> None:
    selected = "gemma4:e2b-it-qat"

    class GenerationUnavailable(FakeOllamaTransport):
        def __call__(
            self,
            method: str,
            url: str,
            payload: dict[str, Any] | None,
            timeout: float,
        ) -> dict[str, Any]:
            if url.endswith("/api/chat"):
                raise OSError("offline")
            return super().__call__(method, url, payload, timeout)

    runtime = StarTransformerRuntime(
        enabled=True,
        transport=GenerationUnavailable(
            models=[{"name": StarTransformerRuntime.MODEL}]
        ),
    )
    service = LocalAiService(tmp_path, transformer_runtime=runtime)

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "test",
                "runtime_model": selected,
                "_runtime_model_selection_authorized": True,
            },
        )
    )

    assert result["ok"] is False
    assert result["selected_runtime_model"] == selected
    assert result["fallback_model_used"] is False
    assert result["error_code"] == "TRANSFORMER_INFERENCE_FAILED"
