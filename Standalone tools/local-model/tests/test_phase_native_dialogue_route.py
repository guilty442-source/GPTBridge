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


def test_short_greeting_uses_first_party_template(monkeypatch, tmp_path) -> None:
    checkpoint = _checkpoint(tmp_path)
    _enable_native(monkeypatch, tmp_path, checkpoint)
    service = _make_service(tmp_path / "tool")

    engine = service.model_engines.main
    result = engine.infer(
        {"instruction": "你好！", "max_output_tokens": 8},
        database={},
        analyze=lambda payload: {},
        search=lambda payload: {},
    )

    assert result["ok"] is True
    assert result["template_reply"] is True
    assert result["generation"]["model_type"] == "first-party-small-talk-template"
    assert "星澄" in result["response"]


def test_regular_prompt_does_not_use_template(monkeypatch, tmp_path) -> None:
    checkpoint = _checkpoint(tmp_path)
    _enable_native(monkeypatch, tmp_path, checkpoint)
    service = _make_service(tmp_path / "tool")

    engine = service.model_engines.main
    result = engine.infer(
        {"instruction": "你是誰？", "max_output_tokens": 8},
        database={},
        analyze=lambda payload: {},
        search=lambda payload: {},
    )

    assert result["ok"] is True
    assert result.get("template_reply") is not True


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


def test_gear_generation_maps_intensity_effort_and_speed() -> None:
    from xingcheng.infrastructure.native_inference import native_gear_generation

    assert native_gear_generation({})[0] == 96  # 自動：依用戶端預算等比縮放
    assert native_gear_generation({"task_intensity": "simple"})[0] == 48
    assert native_gear_generation({"task_intensity": "normal"})[0] == 96
    assert native_gear_generation({"task_intensity": "intermediate"})[0] == 144
    assert native_gear_generation({"task_intensity": "difficult"})[0] == 192
    assert native_gear_generation(
        {"task_intensity": "difficult", "generation_speed": "ultra"}
    )[0] == 96
    assert native_gear_generation({"max_output_tokens": 256})[0] == 48
    _, high_temp = native_gear_generation({"reasoning_effort": "high"})
    _, low_temp = native_gear_generation({"reasoning_effort": "low"})
    assert high_temp == 0.35
    assert low_temp == 0.45
    # 明確 temperature 優先於檔位
    assert native_gear_generation({"reasoning_effort": "high", "temperature": 0.9})[1] is None


def test_gear_reaches_native_engine(monkeypatch, tmp_path) -> None:
    checkpoint = _checkpoint(tmp_path)
    _enable_native(monkeypatch, tmp_path, checkpoint)
    service = _make_service(tmp_path / "tool")

    engine = service.model_engines.main
    simple = engine.infer(
        {"instruction": "你是誰？", "task_intensity": "simple", "min_answer_chars": 0},
        database={},
        analyze=lambda payload: {},
        search=lambda payload: {},
    )
    difficult = engine.infer(
        {"instruction": "你是誰？", "task_intensity": "difficult"},
        database={},
        analyze=lambda payload: {},
        search=lambda payload: {},
    )
    assert simple["generation"]["gear"]["max_tokens"] == 48
    assert difficult["generation"]["gear"]["max_tokens"] == 192


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
