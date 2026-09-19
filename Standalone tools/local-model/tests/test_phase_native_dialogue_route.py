"""星澄原生對話路由：自有權重優先回答（settings 啟用時）。"""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401
from _test_model_registry_helpers import _make_service

import json

import torch

from xingcheng.infrastructure import native_engine as native_engine_module
from xingcheng.infrastructure.native_transformer import (
    XingChengConfig,
    XingChengForCausalLM,
    XingChengTokenizer,
    save_checkpoint,
)


def _checkpoint(tmp_path) -> str:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    torch.manual_seed(3)
    model = XingChengForCausalLM(cfg)
    tokenizer = XingChengTokenizer.from_config(cfg)
    info = save_checkpoint(tmp_path / "native.pt", model, tokenizer=tokenizer)
    return info["path"]


def _enable_native(monkeypatch, tmp_path, checkpoint: str) -> None:
    settings = tmp_path / "native-engine.json"
    settings.write_text(
        json.dumps({"enabled": True, "checkpoint": checkpoint}), encoding="utf-8"
    )
    monkeypatch.setattr(native_engine_module, "settings_path", lambda: settings)
    monkeypatch.setattr(native_engine_module, "_engine_cache", {})
    monkeypatch.setattr(
        native_engine_module,
        "NATIVE_EXECUTION_LEDGER",
        str(tmp_path / "ledger.jsonl"),
    )


def test_native_weights_answer_when_engine_enabled(monkeypatch, tmp_path) -> None:
    checkpoint = _checkpoint(tmp_path)
    _enable_native(monkeypatch, tmp_path, checkpoint)
    service = _make_service(tmp_path / "tool")

    engine = service.model_engines.main
    result = engine.infer(
        {"instruction": "星澄是", "max_output_tokens": 8},
        database={},
        analyze=lambda payload: {},
        search=lambda payload: {},
    )

    assert result["ok"] is True
    assert result["mode"] == "governed-native-transformer-llm"
    assert result["star_native_model_used"] is True
    assert result["third_party_weights_used"] is False
    assert result["generation"]["model_type"] == "native-transformer-autoregressive-decoder"
    assert result["native_checkpoint_path"] == checkpoint
    assert isinstance(result["response"], str)


def test_ngram_path_remains_when_engine_disabled(monkeypatch, tmp_path) -> None:
    settings = tmp_path / "native-engine.json"
    settings.write_text('{"enabled": false}', encoding="utf-8")
    monkeypatch.setattr(native_engine_module, "settings_path", lambda: settings)
    monkeypatch.setattr(native_engine_module, "_engine_cache", {})
    service = _make_service(tmp_path / "tool")

    engine = service.model_engines.main
    result = engine.infer(
        {"instruction": "星澄是", "max_output_tokens": 8},
        database={},
        analyze=lambda payload: {},
        search=lambda payload: {},
    )

    assert result["ok"] is True
    assert result.get("mode") != "governed-native-transformer-llm"
    assert result.get("native_engine") is not True


def test_missing_checkpoint_falls_back_to_ngram(monkeypatch, tmp_path) -> None:
    settings = tmp_path / "native-engine.json"
    settings.write_text(
        json.dumps({"enabled": True, "checkpoint": str(tmp_path / "absent.pt")}),
        encoding="utf-8",
    )
    monkeypatch.setattr(native_engine_module, "settings_path", lambda: settings)
    monkeypatch.setattr(native_engine_module, "_engine_cache", {})
    service = _make_service(tmp_path / "tool")

    engine = service.model_engines.main
    result = engine.infer(
        {"instruction": "星澄是", "max_output_tokens": 8},
        database={},
        analyze=lambda payload: {},
        search=lambda payload: {},
    )

    assert result["ok"] is True
    assert result.get("mode") != "governed-native-transformer-llm"
