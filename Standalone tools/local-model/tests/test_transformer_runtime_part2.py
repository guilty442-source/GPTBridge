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
