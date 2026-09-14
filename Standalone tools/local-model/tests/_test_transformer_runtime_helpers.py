"""Shared test helpers (split from consolidated suite)."""
from __future__ import annotations

import _xingcheng_test_support as _s  # noqa: F401
from _xingcheng_test_support import ROOT

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
