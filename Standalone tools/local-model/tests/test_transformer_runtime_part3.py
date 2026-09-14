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

    assert policy["resident"] == ["qwen3.5:9b-q4_K_M"]
    assert policy["unknown_installed_models"] == "non-resident"
    assert policy["resident_evicted_before_non_resident"] is False
    assert policy["pipeline_release_after_each_stage"] is True
    assert policy["maximum_concurrent_transformers"] == 4


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
                "runtime_model": StarTransformerRuntime.MODEL,
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
                "runtime_model": StarTransformerRuntime.MODEL,
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
                "runtime_model": StarTransformerRuntime.MODEL,
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
