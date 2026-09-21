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
