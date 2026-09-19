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


def test_selected_model_never_invokes_cross_model_frontend_worker(
    tmp_path: Path,
) -> None:
    selected = "llama3.1:8b-instruct-q4_K_M"
    transport = FakeOllamaTransport(
        models=[
            {"name": StarTransformerRuntime.MODEL},
            {"name": StarTransformerRuntime.FRONTEND_WORKER_MODEL},
            {"name": selected},
        ]
    )
    service = LocalAiService(
        tmp_path,
        transformer_runtime=StarTransformerRuntime(enabled=True, transport=transport),
    )

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請用一段 Python 函式計算平均數",
                "runtime_model": selected,
                "_runtime_model_selection_authorized": True,
                "conversation_mode": "coding",
                "task_intensity": "difficult",
                "generation_speed": "medium",
            },
        )
    )

    chat_models = {
        str(call[2].get("model"))
        for call in transport.calls
        if call[1].endswith("/api/chat")
    }
    assert result["ok"] is True
    assert result["generation"]["model"] == selected
    assert chat_models == {selected}
    assert result["model_scheduling"]["automatic"] is False
    assert result["model_scheduling"]["selected_model"] == selected
    assert {
        task["assigned_model"] for task in result["task_arrangement"]["tasks"]
    } == {selected}


def test_selected_star_native_model_answers_with_star(tmp_path: Path) -> None:
    transport = FakeOllamaTransport(
        models=[{"name": StarTransformerRuntime.MODEL}]
    )
    service = LocalAiService(
        tmp_path,
        transformer_runtime=StarTransformerRuntime(
            enabled=True,
            transport=transport,
        ),
    )

    _, result = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請介紹你自己",
                "runtime_model": "star-main-native-model",
                "_runtime_model_selection_authorized": True,
            },
        )
    )

    chat_models = {
        str(call[2].get("model"))
        for call in transport.calls
        if call[1].endswith("/api/chat")
    }
    assert result["ok"] is True
    assert result["model"] == "star-main-native-model"
    assert result["model_name"] == "星澄"
    assert result["model_selection"] == "user-selected"
    assert result["manual_model_selection"] is True
    assert result["selected_runtime_model"] == "star-main-native-model"
    assert result["native_database_access"]["enabled"] is True
    assert result["model_scheduling"]["automatic"] is False
    assert result["model_scheduling"]["selected_model"] == "star-main-native-model"
    assert "transformer_inference" not in result
    assert chat_models == set()


def test_automatic_difficult_coding_still_uses_frontend_worker(
    tmp_path: Path,
) -> None:
    transport = FakeOllamaTransport(
        models=[
            {"name": StarTransformerRuntime.MODEL},
            {"name": StarTransformerRuntime.FRONTEND_WORKER_MODEL},
        ]
    )
    service = LocalAiService(
        tmp_path,
        transformer_runtime=StarTransformerRuntime(enabled=True, transport=transport),
    )

    asyncio.run(
        service.handle(
            "xingcheng_infer",
            {
                "prompt": "請用一段 Python 函式計算平均數",
                "conversation_mode": "coding",
                "task_intensity": "difficult",
                "generation_speed": "medium",
            },
        )
    )

    chat_models = {
        str(call[2].get("model"))
        for call in transport.calls
        if call[1].endswith("/api/chat")
    }
    assert StarTransformerRuntime.FRONTEND_WORKER_MODEL in chat_models


########################################################################
