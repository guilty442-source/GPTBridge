"""Native engine (Phase 1 blueprint): feature-flagged self-trained inference.

Covers: flag default-off, fail-closed missing checkpoint, native generation
through the governed generate() path, and audit-flag consistency.
"""

from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401
from _test_transformer_runtime_helpers import FakeOllamaTransport

import torch
import pytest

from xingcheng.infrastructure.native_engine import (
    NATIVE_CHECKPOINT_ENV,
    NATIVE_ENGINE_ENV,
    NATIVE_MODEL_ID,
    NativeTransformerEngine,
    configured_checkpoint_path,
    flag_enabled,
    generate_via_native_engine,
    native_engine_for,
)
from xingcheng.infrastructure.native_transformer import (
    XingChengConfig,
    XingChengForCausalLM,
    XingChengTokenizer,
    save_checkpoint,
)
from xingcheng.infrastructure.transformer_runtime import StarTransformerRuntime


def _small_config() -> XingChengConfig:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    return cfg


def _write_checkpoint(tmp_path) -> str:
    cfg = _small_config()
    torch.manual_seed(31)
    model = XingChengForCausalLM(cfg)
    tok = XingChengTokenizer.from_config(cfg)
    info = save_checkpoint(tmp_path / "native.pt", model, tokenizer=tok)
    return info["path"]


def test_flag_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv(NATIVE_ENGINE_ENV, raising=False)
    assert flag_enabled() is False


def test_flag_enabled(monkeypatch) -> None:
    monkeypatch.setenv(NATIVE_ENGINE_ENV, "1")
    assert flag_enabled() is True


def test_missing_checkpoint_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(NATIVE_ENGINE_ENV, "1")
    monkeypatch.setenv(NATIVE_CHECKPOINT_ENV, str(tmp_path / "absent.pt"))
    result = generate_via_native_engine({"prompt": "星澄"})
    assert result["ok"] is False
    assert result["error_code"] == "NATIVE_CHECKPOINT_MISSING"
    assert result["fallback_required"] is False


def test_disabled_flag_fails_closed(monkeypatch) -> None:
    monkeypatch.delenv(NATIVE_ENGINE_ENV, raising=False)
    result = generate_via_native_engine({"prompt": "星澄"})
    assert result["ok"] is False
    assert result["error_code"] == "NATIVE_ENGINE_DISABLED"


def test_engine_generates_from_checkpoint(monkeypatch, tmp_path) -> None:
    path = _write_checkpoint(tmp_path)
    monkeypatch.setenv(NATIVE_ENGINE_ENV, "1")
    monkeypatch.setenv(NATIVE_CHECKPOINT_ENV, path)
    result = generate_via_native_engine({"prompt": "星澄", "max_tokens": 4})
    assert result["ok"] is True
    assert result["native_engine"] is True
    assert result["star_native_model_used"] is True
    assert result["third_party_foundation_weights"] is False
    assert result["remote_network_used"] is False
    assert result["model"] == NATIVE_MODEL_ID
    assert result["foundation_model_license"] == "first-party-self-trained"
    assert result["eval_count"] == 4
    assert result["checkpoint_path"] == path


def test_engine_result_is_deterministic_greedy(tmp_path) -> None:
    path = _write_checkpoint(tmp_path)
    engine = NativeTransformerEngine(path)
    first = engine.generate(prompt="星澄", max_tokens=3)
    second = engine.generate(prompt="星澄", max_tokens=3)
    assert first["text"] == second["text"]


def test_runtime_routes_to_native_when_flag_on(monkeypatch, tmp_path) -> None:
    path = _write_checkpoint(tmp_path)
    monkeypatch.setenv(NATIVE_ENGINE_ENV, "1")
    monkeypatch.setenv(NATIVE_CHECKPOINT_ENV, path)
    transport = FakeOllamaTransport(models=[{"name": StarTransformerRuntime.MODEL}])
    runtime = StarTransformerRuntime(enabled=True, transport=transport)
    result = runtime.generate(
        prompt="星澄", intent="conversation", model_role="primary", output={}
    )
    assert result["ok"] is True
    assert result["native_engine"] is True
    assert result["star_native_model_used"] is True
    # 原生路徑不觸碰 Ollama transport
    assert not [call for call in transport.calls if call[1].endswith("/api/chat")]


def test_runtime_flag_off_unchanged(monkeypatch) -> None:
    monkeypatch.delenv(NATIVE_ENGINE_ENV, raising=False)
    monkeypatch.delenv(NATIVE_CHECKPOINT_ENV, raising=False)
    transport = FakeOllamaTransport(models=[{"name": StarTransformerRuntime.MODEL}])
    runtime = StarTransformerRuntime(enabled=True, transport=transport)
    result = runtime.generate(
        prompt="星澄", intent="conversation", model_role="primary", output={}
    )
    assert result.get("native_engine") is not True
    assert result.get("star_native_model_used") is not True


def test_flag_on_missing_checkpoint_does_not_probe_ollama(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(NATIVE_ENGINE_ENV, "1")
    monkeypatch.setenv(NATIVE_CHECKPOINT_ENV, str(tmp_path / "absent.pt"))
    transport = FakeOllamaTransport(models=[{"name": StarTransformerRuntime.MODEL}])
    runtime = StarTransformerRuntime(enabled=True, transport=transport)
    result = runtime.generate(
        prompt="星澄", intent="conversation", model_role="primary", output={}
    )
    assert result["ok"] is False
    assert result["error_code"] == "NATIVE_CHECKPOINT_MISSING"
    assert not transport.calls


def test_checkpoint_env_override_resolution(monkeypatch, tmp_path) -> None:
    path = _write_checkpoint(tmp_path)
    monkeypatch.setenv(NATIVE_CHECKPOINT_ENV, path)
    assert configured_checkpoint_path().name == "native.pt"
    engine = native_engine_for()
    assert engine.checkpoint_path.name == "native.pt"
    with pytest.raises(FileNotFoundError):
        native_engine_for(tmp_path / "missing.pt")
