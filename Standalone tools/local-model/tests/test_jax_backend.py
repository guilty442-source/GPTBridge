from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import pytest

jax = pytest.importorskip("jax")
import jax.numpy as jnp  # noqa: E402

from xingcheng.infrastructure.native_transformer.config import (  # noqa: E402
    XingChengConfig,
)
from xingcheng.infrastructure.native_transformer.jax_backend import (  # noqa: E402
    JaxSFTConfig,
    forward_logits,
    init_params,
    jax_sft_train,
    load_jax_checkpoint,
)


class _StubTokenizer:
    vocab_size = 260
    pad_id = 0
    bos_id = 1
    eos_id = 2

    def encode(self, text, *, add_bos=True, add_eos=False, max_length=None):
        ids = ([self.bos_id] if add_bos else []) + [
            3 + (b % 250) for b in text.encode("utf-8")
        ]
        if add_eos:
            ids.append(self.eos_id)
        if max_length is not None and len(ids) > max_length:
            ids = ids[:max_length]
        return ids


def _config() -> XingChengConfig:
    return XingChengConfig(
        vocab_size=260,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=4,
        max_position_embeddings=128,
    )


def test_init_and_forward_shapes() -> None:
    config = _config()
    params = init_params(config, seed=7)
    logits = forward_logits(params, config, jnp.asarray([[1, 5, 6, 2]]))
    assert logits.shape == (1, 4, 260)


def test_unsupported_config_fails_closed() -> None:
    config = _config()
    object.__setattr__(config, "use_moe", True)
    with pytest.raises(ValueError, match="JAX_BACKEND_UNSUPPORTED"):
        init_params(config)


def test_sft_train_roundtrip(tmp_path) -> None:
    config = _config()
    tok = _StubTokenizer()
    train = [{"prompt": f"q{i}", "completion": f"a{i}"} for i in range(16)]
    val = [{"prompt": "vq", "completion": "va"}]
    summary = jax_sft_train(
        config,
        tok,
        train,
        val,
        JaxSFTConfig(
            max_length=64,
            batch_size=4,
            grad_accum=1,
            max_steps=4,
            warmup_steps=1,
            eval_every=2,
            checkpoint_every=2,
        ),
        output_dir=tmp_path,
    )
    assert summary["framework"] == "jax"
    assert summary["steps"] == 4
    params, manifest = load_jax_checkpoint(tmp_path / "final")
    assert manifest["framework"] == "jax"
    logits = forward_logits(params, config, jnp.asarray([[1, 2, 3]]))
    assert logits.shape[-1] == 260


def test_executor_framework_resolution() -> None:
    from xingcheng.infrastructure import training_job_executor as executor

    assert executor._resolve_train_framework({}) == "jax"
    assert (
        executor._resolve_train_framework({"framework": "torch"}) == "torch"
    )
    with pytest.raises(Exception):
        executor._resolve_train_framework({"framework": "unknown"})
