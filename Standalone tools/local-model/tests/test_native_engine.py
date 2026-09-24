"""Native engine (Phase 1 blueprint): feature-flagged self-trained inference.

Covers: flag default-off, fail-closed missing checkpoint, native generation
through the governed generate() path, and audit-flag consistency.
"""

from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import json

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
from xingcheng.infrastructure.native_runtime import StarNativeRuntime


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


def test_flag_defaults_off(monkeypatch, tmp_path) -> None:
    # Settings 是正式開關路徑；測試以不存在的 settings 檔隔離環境狀態。
    from xingcheng.infrastructure import native_engine as module

    monkeypatch.setattr(module, "settings_path", lambda: tmp_path / "absent.json")
    monkeypatch.delenv(NATIVE_ENGINE_ENV, raising=False)
    assert flag_enabled() is False


def test_flag_enabled(monkeypatch) -> None:
    monkeypatch.setenv(NATIVE_ENGINE_ENV, "1")
    assert flag_enabled() is True


def test_settings_file_enables_engine(monkeypatch, tmp_path) -> None:
    from xingcheng.infrastructure import native_engine as module

    settings = tmp_path / "native-engine.json"
    settings.write_text('{"enabled": true}', encoding="utf-8")
    monkeypatch.setattr(module, "settings_path", lambda: settings)
    monkeypatch.delenv(NATIVE_ENGINE_ENV, raising=False)
    assert flag_enabled() is True


def test_env_off_overrides_enabled_settings(monkeypatch, tmp_path) -> None:
    from xingcheng.infrastructure import native_engine as module

    settings = tmp_path / "native-engine.json"
    settings.write_text('{"enabled": true}', encoding="utf-8")
    monkeypatch.setattr(module, "settings_path", lambda: settings)
    monkeypatch.setenv(NATIVE_ENGINE_ENV, "0")
    assert flag_enabled() is False


def test_checkpoint_from_settings(monkeypatch, tmp_path) -> None:
    from xingcheng.infrastructure import native_engine as module

    settings = tmp_path / "native-engine.json"
    settings.write_text(
        '{"enabled": true, "checkpoint": "runtime/models/pinned.pt"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "settings_path", lambda: settings)
    monkeypatch.delenv(NATIVE_CHECKPOINT_ENV, raising=False)
    assert configured_checkpoint_path() == module.tool_root() / "runtime" / "models" / "pinned.pt"


def test_missing_checkpoint_fails_closed(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(NATIVE_ENGINE_ENV, "1")
    monkeypatch.setenv(NATIVE_CHECKPOINT_ENV, str(tmp_path / "absent.pt"))
    result = generate_via_native_engine({"prompt": "星澄"})
    assert result["ok"] is False
    assert result["error_code"] == "NATIVE_CHECKPOINT_MISSING"
    assert result["fallback_required"] is False


def test_disabled_flag_fails_closed(monkeypatch, tmp_path) -> None:
    from xingcheng.infrastructure import native_engine as module

    monkeypatch.setattr(module, "settings_path", lambda: tmp_path / "absent.json")
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
    runtime = StarNativeRuntime(enabled=True)
    result = runtime.generate(
        prompt="嗨", intent="conversation", model_role="primary", output={}
    )
    assert result["ok"] is True
    assert result["native_engine"] is True
    assert result["star_native_model_used"] is True
    assert result["loopback_runtime_used"] is False

def test_runtime_flag_off_unchanged(monkeypatch, tmp_path) -> None:
    from xingcheng.infrastructure import native_engine as module

    monkeypatch.setattr(module, "settings_path", lambda: tmp_path / "absent.json")
    monkeypatch.delenv(NATIVE_ENGINE_ENV, raising=False)
    monkeypatch.delenv(NATIVE_CHECKPOINT_ENV, raising=False)
    runtime = StarNativeRuntime(enabled=True)
    result = runtime.generate(
        prompt="嗨", intent="conversation", model_role="primary", output={}
    )
    assert result["ok"] is False
    assert result["error_code"] == "NATIVE_ENGINE_DISABLED"
    assert result.get("native_engine") is not True
    assert result.get("star_native_model_used") is not True

def test_flag_on_missing_checkpoint_fails_closed(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(NATIVE_ENGINE_ENV, "1")
    monkeypatch.setenv(NATIVE_CHECKPOINT_ENV, str(tmp_path / "absent.pt"))
    runtime = StarNativeRuntime(enabled=True)
    result = runtime.generate(
        prompt="嗨", intent="conversation", model_role="primary", output={}
    )
    assert result["ok"] is False
    assert result["error_code"] == "NATIVE_CHECKPOINT_MISSING"

def test_checkpoint_env_override_resolution(monkeypatch, tmp_path) -> None:
    path = _write_checkpoint(tmp_path)
    monkeypatch.setenv(NATIVE_CHECKPOINT_ENV, path)
    assert configured_checkpoint_path().name == "native.pt"
    engine = native_engine_for()
    assert engine.checkpoint_path.name == "native.pt"
    with pytest.raises(FileNotFoundError):
        native_engine_for(tmp_path / "missing.pt")


# ── 控制面：生成預設、品質護欄、範圍閘門 ─────────────────────────


def _settings_file(monkeypatch, tmp_path, payload: dict):
    from xingcheng.infrastructure import native_engine as module

    settings = tmp_path / "native-engine.json"
    settings.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(module, "settings_path", lambda: settings)
    return settings


def test_generation_defaults_are_bounded(monkeypatch, tmp_path) -> None:
    from xingcheng.infrastructure import native_engine as module

    _settings_file(
        monkeypatch,
        tmp_path,
        {
            "temperature": 99.0,
            "top_k": -5,
            "top_p": 5.0,
            "repetition_penalty": 0.0,
            "max_new_tokens": 100000,
            "min_answer_chars": -3,
        },
    )
    defaults = module.generation_defaults()
    assert defaults["temperature"] == 2.0
    assert defaults["top_k"] == 0
    assert defaults["top_p"] == 1.0
    assert defaults["repetition_penalty"] == 0.01
    assert defaults["max_new_tokens"] == 512
    assert defaults["min_answer_chars"] == 0


def test_quality_guard_rules() -> None:
    from xingcheng.infrastructure.native_engine import quality_guard

    assert quality_guard("", min_chars=4) == (True, "answer-too-short")
    assert quality_guard("短", min_chars=4) == (True, "answer-too-short")
    assert quality_guard("星澄是本地模型。", min_chars=4) == (False, "")
    assert quality_guard("abc\x00def\x01ghi", min_chars=1) == (True, "answer-garbled")
    assert quality_guard("重複重複" * 10, min_chars=1) == (True, "answer-repetitive")


def test_scope_gate_refuses_out_of_scope(monkeypatch, tmp_path) -> None:
    path = _write_checkpoint(tmp_path)
    _settings_file(
        monkeypatch,
        tmp_path,
        {
            "enabled": True,
            "checkpoint": path,
            "scope_enabled": True,
            "scope_keywords": ["星澄", "治理"],
            "scope_fallback_message": "超出範圍。",
            "max_new_tokens": 4,
        },
    )
    engine = NativeTransformerEngine(path)

    refused = engine.generate(prompt="請解量子力學方程式。", seed=1)
    assert refused["ok"] is True
    assert refused["text"] == "超出範圍。"
    assert refused["scope_guard"]["triggered"] is True
    assert refused["eval_count"] == 0

    allowed = engine.generate(prompt="什麼是治理規則？", max_tokens=2, seed=1)
    assert allowed["scope_guard"]["triggered"] is False
    assert allowed["text"] != "超出範圍。"


def test_cpu_thread_budget_bounded(monkeypatch, tmp_path) -> None:
    from xingcheng.infrastructure import native_engine as module

    # §10.30 unified entry: configured values clamp to the five-core
    # budget cap (was: independent cap of 16).
    from shared_layer.performance.thread_budget import core_budget

    _settings_file(monkeypatch, tmp_path, {"cpu_threads": 64})
    assert module.cpu_thread_budget() == core_budget()
    _settings_file(monkeypatch, tmp_path, {"cpu_threads": -3})
    assert 1 <= module.cpu_thread_budget() <= 4
    _settings_file(monkeypatch, tmp_path, {})
    assert 1 <= module.cpu_thread_budget() <= 4


def test_cpu_generation_cap_setting(monkeypatch, tmp_path) -> None:
    from xingcheng.infrastructure import native_engine as module

    _settings_file(monkeypatch, tmp_path, {"cpu_max_new_tokens": 100000})
    assert module.cpu_generation_cap() == 128
    _settings_file(monkeypatch, tmp_path, {"cpu_max_new_tokens": 0})
    assert module.cpu_generation_cap() == 64  # 0 = 使用預設
    _settings_file(monkeypatch, tmp_path, {})
    assert module.cpu_generation_cap() == 64


def test_cpu_engine_respects_generation_cap(monkeypatch, tmp_path) -> None:
    path = _write_checkpoint(tmp_path)
    _settings_file(
        monkeypatch,
        tmp_path,
        {
            "enabled": True,
            "checkpoint": path,
            "cpu_max_new_tokens": 3,
            "min_answer_chars": 0,
        },
    )
    engine = NativeTransformerEngine(path, device="cpu")
    result = engine.generate(prompt="星澄是", max_tokens=64, seed=1)
    assert result["ok"] is True
    assert result["eval_count"] <= 3


def test_scope_gate_ignores_persona_prefix(monkeypatch, tmp_path) -> None:
    """人格區塊含關鍵詞時，範圍仍只看使用者最新訊息。"""
    from xingcheng.infrastructure import native_engine as module

    _settings_file(
        monkeypatch,
        tmp_path,
        {
            "scope_enabled": True,
            "scope_keywords": ["星澄"],
            "scope_fallback_message": "超出範圍。",
        },
    )
    composed_allowed = (
        "星澄人格設定：\n你是星澄，語氣精確。\n\n"
        "使用者最新訊息：請說明星澄的治理規則。"
    )
    composed_refused = (
        "星澄人格設定：\n你是星澄，語氣精確。\n\n"
        "使用者最新訊息：請解量子力學方程式。"
    )
    assert module.scope_check(composed_allowed) == (False, "")
    assert module.scope_check(composed_refused) == (True, "prompt-out-of-scope")


def test_write_settings_enable_disable(monkeypatch, tmp_path) -> None:
    from xingcheng.infrastructure import native_engine as module

    _settings_file(monkeypatch, tmp_path, {"enabled": False})
    status = module.control_status()
    assert status["enabled"] is False

    module.write_settings(enabled=True)
    assert module.load_settings()["enabled"] is True
    assert module.control_status()["enabled"] is True

    module.write_settings(enabled=False)
    assert module.control_status()["enabled"] is False
