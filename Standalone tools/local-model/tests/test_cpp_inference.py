"""G29/P3 acceptance tests for the formal C++ inference layer."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import torch

SERVICE_ROOT = Path(__file__).resolve().parents[1] / "src" / "backend" / "services"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from xingcheng.infrastructure.native_transformer import cpp_runtime  # noqa: E402
from xingcheng.infrastructure.native_transformer.checkpoint import (  # noqa: E402
    save_checkpoint,
)
from xingcheng.infrastructure.native_transformer.config import (  # noqa: E402
    XingChengConfig,
)
from xingcheng.infrastructure.native_transformer.cpp_export import (  # noqa: E402
    export_checkpoint_for_cpp,
)
from xingcheng.infrastructure.native_transformer.inference.generate import (  # noqa: E402
    Generator,
)
from xingcheng.infrastructure.native_transformer.inference.sampler import (  # noqa: E402
    SamplingConfig,
    Sampler,
)
from xingcheng.infrastructure.native_transformer.modules.model import (  # noqa: E402
    XingChengForCausalLM,
)


def _tiny_config() -> XingChengConfig:
    return XingChengConfig(
        vocab_size=64,
        hidden_size=32,
        intermediate_size=64,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=32,
        max_new_tokens=8,
    )


def _export_tiny_model(tmp_path: Path):
    torch.manual_seed(23)
    config = _tiny_config()
    model = XingChengForCausalLM(config)
    checkpoint = tmp_path / "tiny.pt"
    save_checkpoint(checkpoint, model, config=config)
    bundle = tmp_path / "bundle"
    report = export_checkpoint_for_cpp(checkpoint, bundle)
    return model, config, bundle, report


def test_cpp_engine_logits_match_pytorch(tmp_path: Path) -> None:
    module = cpp_runtime.load_extension()
    model, _config, bundle, report = _export_tiny_model(tmp_path)
    assert report["schema_version"] == "star-native-inference-bundle/v1"

    engine = module.NativeInferenceEngine()
    engine.load(str(bundle))
    ids = [1, 9, 10, 11, 12]
    expected = model(torch.tensor([ids], dtype=torch.long))["logits"][0, -1].double()
    actual = torch.tensor(engine.logits(ids), dtype=torch.float64)
    assert torch.allclose(actual, expected, atol=2e-3, rtol=2e-3)
    # R6 paged KV：stateless logits() 不寫 cache；generate() 後 pool 才按需配置
    assert engine.kv_memory_bytes() == 0
    sampling = module.SamplingConfig()
    sampling.do_sample = False
    sampling.repetition_penalty = 1.0
    engine.generate(ids, 2, sampling)
    assert engine.kv_memory_bytes() > 0
    engine.unload()
    assert not engine.loaded()


def test_cpp_engine_greedy_generation_matches_pytorch(tmp_path: Path) -> None:
    module = cpp_runtime.load_extension()
    model, _config, bundle, _report = _export_tiny_model(tmp_path)
    engine = module.NativeInferenceEngine()
    engine.load(str(bundle))
    prompt = torch.tensor([[1, 9, 10, 11]], dtype=torch.long)
    sampling = SamplingConfig(do_sample=False, repetition_penalty=1.0)
    expected = Generator(
        model, sampler=Sampler(sampling), device=torch.device("cpu")
    ).generate(
        prompt, max_new_tokens=4, use_cache=True
    )[0].tolist()
    cpp_sampling = module.SamplingConfig()
    cpp_sampling.do_sample = False
    cpp_sampling.repetition_penalty = 1.0
    actual = engine.generate([1, 9, 10, 11], 4, cpp_sampling)
    assert actual == expected


# G41 dual-path parity contract: the C++ engine's hidden stream must track
# the PyTorch reference at every stage (embedding → each transformer layer →
# final norm). RMS is compared per stage; fp32 (torch) vs fp64 (C++) drift
# is bounded by this tolerance so a swapped/miswired layer cannot hide
# behind a passing end-to-end logits check.
LAYER_PARITY_RTOL = 2e-2
LAYER_PARITY_ATOL = 1e-3


def _torch_layer_rms(model: XingChengForCausalLM, ids: list[int]) -> list[float]:
    """RMS of the hidden stream per stage, in forward order."""
    metrics: list[float] = []
    hooks = []

    def _rms(tensor: torch.Tensor) -> float:
        return float(tensor.double().pow(2).mean().sqrt())

    def _hook(_module, _inputs, output):
        hidden = output[0] if isinstance(output, tuple) else output
        metrics.append(_rms(hidden))

    with torch.no_grad():
        batch = torch.tensor([ids], dtype=torch.long)
        metrics.append(_rms(model.model.embeddings(batch)))
        for layer in model.model.layers:
            hooks.append(layer.register_forward_hook(_hook))
        hooks.append(model.model.final_norm.register_forward_hook(_hook))
        try:
            model(batch)
        finally:
            for hook in hooks:
                hook.remove()
    return metrics


def test_cpp_layerwise_parity_with_pytorch(tmp_path: Path) -> None:
    """G41: per-stage hidden RMS must match the PyTorch path."""
    model, _config, bundle, _report = _export_tiny_model(tmp_path)
    engine = cpp_runtime.load_extension().NativeInferenceEngine()
    engine.load(str(bundle))

    ids = [1, 9, 10, 11, 12]
    expected = _torch_layer_rms(model, ids)
    actual = list(engine.layer_metrics(ids))

    assert len(actual) == _config.num_hidden_layers + 2
    assert len(actual) == len(expected)
    drift = [
        abs(a - e) / max(abs(e), LAYER_PARITY_ATOL)
        for a, e in zip(actual, expected)
    ]
    assert all(
        abs(a - e) <= LAYER_PARITY_ATOL + LAYER_PARITY_RTOL * abs(e)
        for a, e in zip(actual, expected)
    ), f"layerwise parity drift {drift} exceeds contract"


def test_cpp_bundle_hash_and_bounds_fail_closed(tmp_path: Path) -> None:
    module = cpp_runtime.load_extension()
    _model, _config, bundle, _report = _export_tiny_model(tmp_path)
    weights = bundle / "weights.bin"
    data = bytearray(weights.read_bytes())
    data[-1] ^= 0x01
    weights.write_bytes(data)

    engine = module.NativeInferenceEngine()
    with pytest.raises(RuntimeError, match="SHA256_MISMATCH"):
        engine.load(str(bundle))


def test_cpp_byte_level_bpe_tokenizer(tmp_path: Path) -> None:
    module = cpp_runtime.load_extension()
    tokenizer_json = tmp_path / "tokenizer.json"
    tokenizer_json.write_text(
        json.dumps(
            {
                "model": {
                    "vocab": {
                        "<|pad|>": 0,
                        "<|bos|>": 1,
                        "<|eos|>": 2,
                        "<|unk|>": 3,
                        "A": 9,
                        "B": 10,
                        "AB": 11,
                    },
                    "merges": ["A B"],
                }
            }
        ),
        encoding="utf-8",
    )
    tokenizer = module.NativeInferenceEngine()
    # Tokenizer is exercised through a minimal engine-free surface by loading a
    # bundle whose manifest points at tokenizer.json.
    model, _config, bundle, _report = _export_tiny_model(tmp_path / "tok")
    del model
    (bundle / "tokenizer.json").write_text(tokenizer_json.read_text(encoding="utf-8"), encoding="utf-8")
    tokenizer.load(str(bundle))
    ids = tokenizer.encode("AB", add_bos=True, add_eos=True)
    assert ids == [1, 11, 2]
    assert tokenizer.decode(ids) == "AB"


def test_cpp_layerwise_parity_with_pytorch(tmp_path: Path) -> None:
    """G41: compare embedding/layer/final-norm RMS traces, not only logits."""
    module = cpp_runtime.load_extension()
    model, config, bundle, _report = _export_tiny_model(tmp_path)
    model.eval()
    ids = [1, 9, 10, 11, 12]
    input_ids = torch.tensor([ids], dtype=torch.long)
    trace: list[float] = []

    def capture(_module, _inputs, output):
        value = output[0] if isinstance(output, tuple) else output
        trace.append(float(value.detach().double().pow(2).mean().sqrt()))

    hooks = [model.model.embeddings.register_forward_hook(capture)]
    hooks.extend(layer.register_forward_hook(capture) for layer in model.model.layers)
    hooks.append(model.model.final_norm.register_forward_hook(capture))
    try:
        with torch.no_grad():
            model.model(input_ids)
    finally:
        for hook in hooks:
            hook.remove()

    engine = module.NativeInferenceEngine()
    engine.load(str(bundle))
    native_trace = engine.layer_metrics(ids)
    engine.unload()

    assert len(native_trace) == len(trace) == config.num_hidden_layers + 2
    assert torch.allclose(
        torch.tensor(native_trace, dtype=torch.float64),
        torch.tensor(trace, dtype=torch.float64),
        atol=1e-3,
        rtol=2e-2,
    )


def test_cpp_generated_output_parser() -> None:
    module = cpp_runtime.load_extension()
    parsed = module.parse_generated_output(
        '回答<|eot|><tool_call>{"name":"search","arguments":{"q":"x"}}</tool_call>'
    )
    payload = json.loads(parsed)
    assert payload["schema"] == "star-inference-output/v1"
    assert payload["text"] == "回答"
    assert payload["tool_call"]["name"] == "search"
    with pytest.raises(RuntimeError, match="TOOL_CALL"):
        module.parse_generated_output("<tool_call>{not-json}</tool_call>")


# ── G29 路由層：governed feature flag 與資源接線 ────────────────────────


def _tiny_tokenizer():
    import tokenizers

    vocab = {
        "<|pad|>": 0,
        "<|bos|>": 1,
        "<|eos|>": 2,
        "<|unk|>": 3,
        "<|system|>": 4,
        "<|user|>": 5,
        "<|assistant|>": 6,
        "<|tool|>": 7,
        "<|eot|>": 8,
        "A": 9,
        "B": 10,
        "C": 11,
        "AB": 12,
    }
    backend = tokenizers.Tokenizer(
        tokenizers.models.BPE(vocab=vocab, merges=[("A", "B")])
    )
    from xingcheng.infrastructure.native_transformer.bpe import (
        NativeBPETokenizer,
    )

    return NativeBPETokenizer(backend, backend.get_vocab_size())


def _tiny_checkpoint(tmp_path: Path) -> Path:
    torch.manual_seed(23)
    config = _tiny_config()
    model = XingChengForCausalLM(config)
    checkpoint = tmp_path / "tiny.pt"
    save_checkpoint(
        checkpoint, model, config=config, tokenizer=_tiny_tokenizer()
    )
    return checkpoint


def _patched_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    from xingcheng.infrastructure import native_engine

    monkeypatch.setattr(native_engine, "tool_root", lambda: tmp_path)
    monkeypatch.setattr(cpp_runtime, "tool_root", lambda: tmp_path)
    return native_engine


def test_cpp_runtime_mode_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cpp_runtime.CPP_RUNTIME_ENV, "off")
    assert cpp_runtime.cpp_runtime_mode() == "off"
    monkeypatch.setenv(cpp_runtime.CPP_RUNTIME_ENV, "required")
    assert cpp_runtime.cpp_runtime_mode() == "required"
    monkeypatch.setenv(cpp_runtime.CPP_RUNTIME_ENV, "fallback")
    assert cpp_runtime.cpp_runtime_mode() == "fallback"
    monkeypatch.setenv(cpp_runtime.CPP_RUNTIME_ENV, "nonsense")
    assert cpp_runtime.cpp_runtime_mode() == "invalid"


def test_generate_via_native_engine_cpp_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_engine = _patched_roots(monkeypatch, tmp_path)
    checkpoint = _tiny_checkpoint(tmp_path)
    monkeypatch.setenv("XINGCHENG_NATIVE_ENGINE", "1")
    monkeypatch.setenv("XINGCHENG_NATIVE_CHECKPOINT", str(checkpoint))
    monkeypatch.setenv(cpp_runtime.CPP_RUNTIME_ENV, "required")

    result = native_engine.generate_via_native_engine(
        {"prompt": "AB", "max_tokens": 4, "seed": 7}
    )
    assert result["ok"] is True
    assert result["decoder"] == "xingcheng-cpp-inference-engine"
    assert result["cpp_runtime"] is True
    assert result["device"] == "cpu"
    assert result["third_party_foundation_weights"] is False

    ledger = (
        tmp_path
        / "xingcheng"
        / "runtime"
        / "logs"
        / "native-engine-executions.jsonl"
    )
    entries = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
    ]
    cpp_entries = [e for e in entries if e.get("engine") == "cpp"]
    assert cpp_entries and cpp_entries[-1]["event"] == "cpp-runtime-execution"


def test_generate_via_native_engine_cpp_invalid_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_engine = _patched_roots(monkeypatch, tmp_path)
    monkeypatch.setenv("XINGCHENG_NATIVE_ENGINE", "1")
    monkeypatch.setenv(cpp_runtime.CPP_RUNTIME_ENV, "invalid-mode")
    result = native_engine.generate_via_native_engine({"prompt": "AB"})
    assert result["ok"] is False
    assert result["error_code"] == "CPP_RUNTIME_MODE_INVALID"


def test_generate_via_native_engine_cpp_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_engine = _patched_roots(monkeypatch, tmp_path)
    checkpoint = _tiny_checkpoint(tmp_path)
    monkeypatch.setenv("XINGCHENG_NATIVE_ENGINE", "1")
    monkeypatch.setenv("XINGCHENG_NATIVE_CHECKPOINT", str(checkpoint))
    monkeypatch.setenv(cpp_runtime.CPP_RUNTIME_ENV, "fallback")

    def _missing_extension():
        raise ImportError("extension intentionally unavailable")

    monkeypatch.setattr(cpp_runtime, "load_extension", _missing_extension)
    result = native_engine.generate_via_native_engine(
        {"prompt": "AB", "max_tokens": 4, "seed": 7}
    )
    assert result["ok"] is True
    assert result["decoder"] == "native-transformer-autoregressive-decoder"

    ledger = (
        tmp_path
        / "xingcheng"
        / "runtime"
        / "logs"
        / "native-engine-executions.jsonl"
    )
    entries = [
        json.loads(line)
        for line in ledger.read_text(encoding="utf-8").splitlines()
    ]
    assert any(e.get("event") == "cpp-runtime-fallback" for e in entries)


def test_ensure_bundle_reuse_and_stale_reexport(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patched_roots(monkeypatch, tmp_path)
    checkpoint = _tiny_checkpoint(tmp_path)
    first = cpp_runtime.ensure_bundle(checkpoint)
    second = cpp_runtime.ensure_bundle(checkpoint)
    assert first["output_dir"] == second["output_dir"]
    assert second["reused"] is True

    torch.manual_seed(41)
    model = XingChengForCausalLM(_tiny_config())
    save_checkpoint(
        checkpoint, model, config=_tiny_config(), tokenizer=_tiny_tokenizer()
    )
    third = cpp_runtime.ensure_bundle(checkpoint)
    assert third["reused"] is False


def test_cpp_engine_kv_limit_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patched_roots(monkeypatch, tmp_path)
    checkpoint = _tiny_checkpoint(tmp_path)
    bundle_dir = Path(cpp_runtime.ensure_bundle(checkpoint)["output_dir"])
    # max KV = layers*positions*kv_heads*head_dim*2*8 bytes; deny half of it.
    limit = 2 * 32 * 2 * 8 * 8 - 1
    with pytest.raises(RuntimeError, match="KV_MEMORY_LIMIT_EXCEEDED"):
        cpp_runtime.load_engine(bundle_dir, kv_memory_limit=limit)


def test_cpp_kv_paged_allocation_on_demand(tmp_path: Path) -> None:
    """R6：KV pool 按 16-token block 分頁，只配置實際用量而非最差值。"""
    module = cpp_runtime.load_extension()
    _model, config, bundle, _report = _export_tiny_model(tmp_path)
    engine = module.NativeInferenceEngine()
    engine.load(str(bundle))
    kv_dim = config.num_key_value_heads * config.head_dim
    block_bytes = config.num_hidden_layers * 16 * kv_dim * 8 * 2  # K+V
    worst = (
        config.num_hidden_layers * config.max_position_embeddings * kv_dim * 8 * 2
    )
    sampling = module.SamplingConfig()
    sampling.do_sample = False
    sampling.repetition_penalty = 1.0
    engine.generate([1, 9, 10, 11, 12], 4, sampling)  # ≤9 positions → 1 block
    used = engine.kv_memory_bytes()
    assert used == block_bytes
    assert used < worst


def test_cpp_prefix_cache_reuse_is_deterministic(tmp_path: Path) -> None:
    module = cpp_runtime.load_extension()
    _model, _config, bundle, _report = _export_tiny_model(tmp_path)
    engine = module.NativeInferenceEngine()
    engine.load(str(bundle))
    sampling = module.SamplingConfig()
    sampling.do_sample = False
    sampling.repetition_penalty = 1.0

    prompt = [1, 9, 10, 11]
    first = engine.generate(list(prompt), 4, sampling)
    second = engine.generate(list(prompt), 4, sampling)
    assert first == second
    stats = json.loads(engine.describe())
    assert stats["prefix_cache_hits"] == 1
    assert stats["prefix_cache_entries"] == 1

    # Partial-prefix hit must match a cold engine bitwise.
    extended = [1, 9, 10, 11, 12]
    cached = engine.generate(list(extended), 4, sampling)
    stats = json.loads(engine.describe())
    assert stats["prefix_cache_hits"] == 2

    cold = module.NativeInferenceEngine()
    cold.load(str(bundle))
    baseline = cold.generate(list(extended), 4, sampling)
    assert cached == baseline
