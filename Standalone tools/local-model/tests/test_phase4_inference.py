"""Phase 4: per-request sampling params, sliding window, INT8, benchmark."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import torch
import pytest

from xingcheng.infrastructure.native_engine import (
    NativeTransformerEngine,
    generate_via_native_engine,
)
from xingcheng.infrastructure.native_transformer import (
    XingChengConfig,
    XingChengForCausalLM,
    XingChengTokenizer,
    save_checkpoint,
)
from xingcheng.infrastructure.native_transformer.benchmark import (
    compare_perplexity,
    run_benchmark,
)


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


def test_per_request_sampling_params_reported(tmp_path) -> None:
    engine = NativeTransformerEngine(_write_checkpoint(tmp_path))
    result = engine.generate(
        prompt="hello world",
        max_tokens=8,
        temperature=0.8,
        top_k=16,
        top_p=0.9,
        repetition_penalty=1.2,
        seed=7,
    )

    assert result["ok"] is True
    sampling = result["sampling"]
    assert sampling["do_sample"] is True
    assert sampling["top_p"] == 0.9
    assert sampling["repetition_penalty"] == 1.2
    assert sampling["seed"] == 7
    assert result["prompt_truncated"] is False


def test_seed_makes_sampling_deterministic(tmp_path) -> None:
    engine = NativeTransformerEngine(_write_checkpoint(tmp_path))
    first = engine.generate(
        prompt="hello", max_tokens=16, temperature=1.0, top_p=0.8, seed=99
    )
    second = engine.generate(
        prompt="hello", max_tokens=16, temperature=1.0, top_p=0.8, seed=99
    )

    assert first["text"] == second["text"]


def test_sliding_window_truncates_oversized_prompt(tmp_path) -> None:
    engine = NativeTransformerEngine(_write_checkpoint(tmp_path))
    long_prompt = "word " * 500

    blocked = engine.generate(prompt=long_prompt, max_tokens=4)
    assert blocked["ok"] is False
    assert blocked["error_code"] == "NATIVE_ENGINE_PROMPT_TOO_LONG"

    truncated = engine.generate(
        prompt=long_prompt, max_tokens=4, sliding_window=True
    )
    assert truncated["ok"] is True
    assert truncated["prompt_truncated"] is True
    assert truncated["prompt_eval_count"] <= 64


def test_request_passthrough_carries_phase4_params(
    tmp_path, monkeypatch
) -> None:
    checkpoint = _write_checkpoint(tmp_path)
    monkeypatch.setenv("XINGCHENG_NATIVE_ENGINE", "1")
    monkeypatch.setenv("XINGCHENG_NATIVE_CHECKPOINT", checkpoint)

    result = generate_via_native_engine(
        {
            "prompt": "hi",
            "max_tokens": 4,
            "top_p": 0.5,
            "repetition_penalty": 1.5,
            "seed": 3,
            "sliding_window": False,
        }
    )
    assert result["ok"] is True
    assert result["sampling"]["top_p"] == 0.5
    assert result["sampling"]["repetition_penalty"] == 1.5


def test_int8_quantized_engine_reports_quantization(tmp_path) -> None:
    engine = NativeTransformerEngine(
        _write_checkpoint(tmp_path), quantize=8
    )
    result = engine.generate(prompt="hello", max_tokens=4)

    assert result["ok"] is True
    assert result["quantization"] == "int8"
    assert result["parameter_count"] > 0


def test_benchmark_report_is_reproducible(tmp_path) -> None:
    checkpoint = _write_checkpoint(tmp_path)
    report = run_benchmark(
        checkpoint, prompt="benchmark", max_new_tokens=8, repeat=2, seed=5
    )

    assert report["format_version"] == "star-native-benchmark/v1"
    assert report["quantization"] == "none"
    assert len(report["latency_ms"]) == 2
    assert report["tokens_per_second"] > 0
    assert report["remote_network_used"] is False
    assert len(report["checkpoint_sha256"]) == 64


def test_benchmark_int8_perplexity_comparison(tmp_path) -> None:
    checkpoint = _write_checkpoint(tmp_path)
    comparison = compare_perplexity(
        checkpoint, "hello world benchmark text " * 40
    )

    assert comparison["baseline_perplexity"] > 0
    assert comparison["int8_perplexity"] > 0
    assert comparison["relative_delta"] is not None
    assert "within_5_percent" in comparison
