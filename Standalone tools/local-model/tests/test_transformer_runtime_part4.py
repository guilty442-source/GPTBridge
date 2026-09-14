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



########################################################################
