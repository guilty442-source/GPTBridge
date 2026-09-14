"""Shared test helpers (split from consolidated suite)."""
from __future__ import annotations

import _xingcheng_test_support as _s  # noqa: F401
from _xingcheng_test_support import ROOT

import asyncio
import sqlite3
import sys
from pathlib import Path
from typing import Any
import pytest
from xingcheng.domain.model_registry import StarModelRegistry
from xingcheng.domain.module_registry import StarModuleRegistry
from xingcheng.integration.memory_broker import StarMemoryBroker
from xingcheng.infrastructure.repository import LocalAiRepository
from xingcheng.infrastructure.ollama_model_repository import OllamaModelRepository
from xingcheng.infrastructure.transformer_runtime import StarTransformerRuntime
from xingcheng.infrastructure.model_engines import StarModelEngines
from xingcheng.infrastructure.generative_language_model import (
    StarAutoregressiveLanguageModel,
)
from xingcheng.infrastructure.native_model import StarNativeLanguageModel
from xingcheng.infrastructure import repository as repository_module
from xingcheng.application.service import LocalAiService
from xingcheng.infrastructure.market_data import (
    MarketDataSearch,
    _fund_query_terms,
    _yahoo_symbol_candidates,
    market_source_catalog,
    recognize_holding_identity,
)
from xingcheng.application.investment_accounting import coordinate_investment_accounting
from xingcheng.application.investment_analysis import ANALYSIS_MODEL_KEYS, analyze_investments
from xingcheng.application.coding_expert import StarCodingExpert


def _make_service(tool_root: Path) -> LocalAiService:
    """Create a LocalAiService with transformer disabled for native-model tests.

    These tests verify the native generative language model and specialist
    routing behavior.  The transformer (Ollama) runtime is disabled so
    tests don't depend on a live Ollama server.
    """
    return LocalAiService(tool_root, enable_transformer=False)


class _FakeOllamaTransport:
    """Minimal fake Ollama transport for tests that need transformer enabled."""

    def __init__(self, response_text: str = "我是星澄的本機 Transformer 語言模型。") -> None:
        self.response_text = response_text
        self.calls: list[tuple[str, str, dict[str, Any] | None, float]] = []

    def __call__(self, method: str, url: str, payload: dict[str, Any] | None, timeout: float) -> dict[str, Any]:
        self.calls.append((method, url, payload, timeout))
        if url.endswith("/api/tags"):
            return {"models": [{"name": StarTransformerRuntime.MODEL}]}
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
            return {"embeddings": [[1.0, float(i + 1)] for i, _ in enumerate(inputs if isinstance(inputs, list) else [])]}
        raise AssertionError(f"unexpected URL: {url}")


def _make_transformer_service(tool_root: Path, response_text: str = "星澄本機模型回答。") -> LocalAiService:
    """Create a LocalAiService with a fake transformer transport for tests that need transformer enabled."""
    runtime = StarTransformerRuntime(enabled=True, transport=_FakeOllamaTransport(response_text))
    return LocalAiService(tool_root, transformer_runtime=runtime)
