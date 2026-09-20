"""Split from consolidated test_xingcheng.py (local-model/tests/test_transformer_runtime.py)."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401
from _xingcheng_test_support import ROOT
from _test_transformer_runtime_helpers import FakeOllamaTransport

import asyncio
import json
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any
import pytest
from xingcheng.application.service import LocalAiService
from xingcheng.infrastructure.transformer_runtime import StarTransformerRuntime
from xingcheng.infrastructure.transformer_runtime_catalog import (
    TransformerRuntimeCatalog,
)

import asyncio
import json
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]

from xingcheng.application.service import LocalAiService
from xingcheng.infrastructure.transformer_runtime import StarTransformerRuntime
from xingcheng.infrastructure.transformer_runtime_catalog import (
    TransformerRuntimeCatalog,
)


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

    assert manifest["id"] == "local-model"
    assert manifest["independent_tool"] is True
    assert manifest["main_system_independent_tool"] is True
    assert manifest["canonical_source_root"] == "local-model"
    assert manifest["physical_owner_root"] == "local-model"
    assert manifest["shared_permission_owner"] == "local-model"
    assert manifest["shared_permission_profile"] == (
        LocalAiService.PLATFORM_PERMISSION_PROFILE
    )
    permissions = manifest["permissions"]
    assert permissions["profile"] == LocalAiService.PLATFORM_PERMISSION_PROFILE
    assert permissions["business_permission_owner"] == "local-model"
    assert permissions["settings_owner"] == "local-model"
    assert permissions["code_scope"] == "tool-root-only"
    assert permissions["database_scope"] == "tool-database-only"
    assert permissions["canonical_database_scope"] == (
        "xingcheng-shared-repository"
    )
    assert "direct-database-write" in permissions["deny"]
    assert "separate-business-database" in permissions["deny"]
    assert "separate-settings-layer" in permissions["deny"]
    assert {item["id"] for item in manifest["companion_tools"]} == {
        "xingcheng",
        "model-dialogue",
    }
    assert all(
        item["companion_owner"] == "local-model"
        for item in manifest["companion_tools"]
    )
    star_permissions = manifest["capabilities"]["xingcheng"][
        "star_native_model_permissions"
    ]
    assert star_permissions["database_write"] is True
    assert star_permissions["investment_database_write"] is True
    assert star_permissions["database_write_scope"] == (
        "xingcheng-model-internal-unrestricted-excluding-permission-data"
    )
    assert LocalAiService.PLATFORM_MODE == "context-aware-multitask-model-platform"
    assert LocalAiService.ENTRY_GATEWAY == "all-ai-business-entries"
    assert LocalAiService.CENTRAL_MANAGEMENT_RESPONSIBILITIES == (
        "sql-central-management",
        "rag-central-management",
        "git-central-management",
    )
    assert LocalAiService.STAR_NATIVE_MODEL_PERMISSIONS[
        "governance_source_access"
    ] == "direct-read-only-authoritative"
    assert {item["id"] for item in LocalAiService.PLATFORM_LABELS} == {
        "ai-entry-gateway",
        "context-multitask",
        "local-model-routing",
        "selected-model-direct",
        "ollama-loopback",
        "governance-controlled",
    }
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
        intent="conversation",
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
    assert chat[2]["options"]["num_ctx"] == 2_048
    assert "優先使用繁體中文" in chat[2]["messages"][0]["content"]
    assert result["residency"] == "non-resident"


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
    # Non-resident models stay warm (keep_alive floor) so back-to-back
    # requests do not pay a full reload; lazy eviction in prepare_model
    # handles real memory pressure instead.
    assert chat[2]["keep_alive"] == "5m"
    assert result["parameter_profile"]["context_limit"] == 153_600


def test_commander_stays_resident_and_uses_low_load_daily_profile() -> None:
    selected = TransformerRuntimeCatalog.MODEL
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
    assert runtime.status()["residency_policy"]["resident"] == [selected]
    assert runtime.COMMANDER_MAX_PARALLEL == 1
    assert chat[2]["keep_alive"] == runtime.RESIDENT_KEEP_ALIVE
    assert chat[2]["options"]["num_ctx"] == runtime.MIN_CONTEXT_WINDOW
    assert chat[2]["options"]["num_predict"] == 1_024
    assert chat[2]["think"] is False


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
    assert result["parameter_profile"]["context_limit"] == 153_600


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
    assert chats[1][2]["options"]["num_predict"] == 4_096
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
        transport=FakeOllamaTransport(
            "建議投入NT$999,999。",
            models=[{"name": "deepseek-r1:14b"}],
        ),
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
    assert chat[2]["keep_alive"] == "5m"
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
    assert embed_call[2]["keep_alive"] == "10m"


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
        "lightweight-tool-reasoning"
    )
    assert catalog["nemotron-3-nano:4b"]["context_window"] == 262_144
    assert catalog["qwen2.5-coder:7b"]["usage_class"] == (
        "fast-coding-and-command-execution"
    )
    assert catalog["qwen2.5-coder:7b"]["context_window"] == 32_768
    assert runtime.preferred_model_for_intent("command_understanding", "low") == (
        "qwen3.8:27b-q4_K_M"
    )
    assert runtime.preferred_model_for_intent("coding", "low") == (
        "granite-code:3b"
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


def test_automatic_search_routing_uses_installed_search_model() -> None:
    search_model = "mistral-small:24b"
    transport = FakeOllamaTransport(
        models=[{"name": StarTransformerRuntime.MODEL}, {"name": search_model}]
    )
    runtime = StarTransformerRuntime(enabled=True, transport=transport)

    result = runtime.generate(
        prompt="搜尋資料",
        intent="search",
        model_role="daily-primary",
        output={"response": "搜尋結果"},
    )

    assert result["ok"] is True
    assert result["model"] == search_model
    assert result["model_selected_by_user"] is False


def test_automatic_math_routing_uses_deepseek_owner_with_safe_context() -> None:
    math_model = TransformerRuntimeCatalog.INTENT_MODEL_PREFERENCES["calculation"][0]
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
    assert result["context_window"] == runtime.SAFE_CONTEXT_WINDOW
    assert result["reasoning_effort"] == "high"
    chat = next(call for call in transport.calls if call[1].endswith("/api/chat"))
    assert chat[2]["options"]["num_ctx"] == runtime.SAFE_CONTEXT_WINDOW
    assert chat[2]["think"] is True
