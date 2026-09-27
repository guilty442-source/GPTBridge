"""Retrace-bounding invariants for the JAX SFT step (A612 framework).

Pins the 2026-09-27 optimisation contract on
``jax_backend/sft.py``:

- ``_collate`` pads to fixed 64-token buckets instead of batch-max, so
  the number of distinct compiled XLA shapes is bounded by
  ``ceil(max_length / 64)`` — not by the number of distinct sequence
  lengths in the data;
- the whole micro-batch (loss + grads + clip + AdamW) is fused into ONE
  ``jax.jit`` step with ``donate_argnums`` for the resident pytrees;
- ``lr`` is a traced scalar argument, not a closure constant — the lr
  schedule must not force a recompile every step;
- the eval loss runs jitted as well.
"""
from __future__ import annotations

import inspect

import _xingcheng_test_support as _support  # noqa: F401
import pytest

jax = pytest.importorskip("jax")

from xingcheng.infrastructure.native_transformer.config import (  # noqa: E402
    XingChengConfig,
)
from xingcheng.infrastructure.native_transformer.jax_backend import (  # noqa: E402
    JaxSFTConfig,
    jax_sft_train,
)
from xingcheng.infrastructure.native_transformer.jax_backend import (  # noqa: E402
    sft,
)


def test_collate_pads_to_fixed_bucket() -> None:
    batch = [(list(range(10)), list(range(10))), (list(range(50)), list(range(50)))]
    inputs, labels = sft._collate(batch, pad_id=0, max_length=256)
    assert inputs.shape[1] == sft._COLLATE_BUCKET
    assert labels.shape == inputs.shape


def test_collate_bucket_is_multiples_of_64() -> None:
    for width, expected in ((1, 64), (64, 64), (65, 128), (129, 192)):
        batch = [(list(range(width)), list(range(width)))]
        inputs, _ = sft._collate(batch, pad_id=0, max_length=256)
        assert inputs.shape[1] == expected


def test_collate_caps_at_max_length() -> None:
    # width 65 rounds up to 128, but max_length=96 caps the bucket.
    batch = [(list(range(65)), list(range(65)))]
    inputs, _ = sft._collate(batch, pad_id=0, max_length=96)
    assert inputs.shape[1] == 96


def test_collate_bounds_compiled_shapes() -> None:
    """Every width in 1..max_length must land in ceil(max_length/64)
    distinct bucket widths — the retrace bound the optimisation claims."""
    seen = set()
    for width in range(1, 257):
        batch = [(list(range(width)), list(range(width)))]
        inputs, _ = sft._collate(batch, pad_id=0, max_length=256)
        seen.add(inputs.shape[1])
    assert seen == {64, 128, 192, 256}


def test_train_and_eval_steps_are_jitted(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Run a 2-step training with a recording ``jax.jit`` wrapper and
    verify the fused step (donated pytrees, traced ``lr``) plus the eval
    loss both went through jit."""
    recorded: list[tuple[object, dict]] = []
    real_jit = jax.jit

    def _spy(fun, *args, **kwargs):
        recorded.append((fun, kwargs))
        return real_jit(fun, *args, **kwargs)

    monkeypatch.setattr(jax, "jit", _spy)

    config = XingChengConfig(
        vocab_size=260,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=4,
        max_position_embeddings=128,
    )

    class _Tok:
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
            if max_length is not None:
                ids = ids[:max_length]
            return ids

    summary = jax_sft_train(
        config,
        _Tok(),
        [{"prompt": f"q{i}", "completion": f"a{i}"} for i in range(8)],
        [{"prompt": "vq", "completion": "va"}],
        JaxSFTConfig(
            max_length=64, batch_size=4, grad_accum=1,
            max_steps=2, warmup_steps=1,
        ),
        output_dir=tmp_path,
    )
    assert summary["framework"] == "jax"
    assert summary["steps"] == 2

    # Fused step: donated params+opt_state and a traced lr scalar.
    fused = [
        (fn, kw) for fn, kw in recorded
        if kw.get("donate_argnums") == (0, 1)
    ]
    assert fused, "train step must be jitted with donate_argnums=(0, 1)"
    assert any(
        "lr" in inspect.signature(fn).parameters for fn, _ in fused
    ), "lr must be a traced argument of the fused step, not a closure constant"

    # Eval path jitted too (the train-step jit plus at least one more).
    assert len(recorded) >= 2


def test_choose_bucket_least_waste_within_shape_bound() -> None:
    # Narrow short corpus (all lengths <= 48): bucket 16 stays within
    # the 8-shape bound and wastes far less padding than 64.
    short = [10 + i % 40 for i in range(64)]
    assert sft._choose_bucket(short, max_length=64) == 16
    # Wide 512-token corpus: bucket 16 would yield ~32 distinct widths,
    # so the bound forces a coarser bucket (64 -> exactly 8 widths).
    wide = [i % 512 + 1 for i in range(512)]
    chosen = sft._choose_bucket(wide, max_length=512)
    assert chosen == 64


def test_choose_bucket_respects_max_shapes() -> None:
    lengths = list(range(1, 513))
    bucket = sft._choose_bucket(lengths, max_length=512, max_shapes=4)
    widths = {
        min(512, max(bucket, bucket * -(-n // bucket))) for n in lengths
    }
    assert len(widths) <= 4


def test_config_explicit_bucket_wins_and_shapes_reported(
    tmp_path,
) -> None:
    config = XingChengConfig(
        vocab_size=260,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=4,
        max_position_embeddings=128,
    )

    class _Tok:
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
            if max_length is not None:
                ids = ids[:max_length]
            return ids

    summary = jax_sft_train(
        config,
        _Tok(),
        [{"prompt": f"q{i}", "completion": f"a{i}"} for i in range(8)],
        [{"prompt": "vq", "completion": "va"}],
        JaxSFTConfig(
            max_length=64, batch_size=4, grad_accum=1,
            max_steps=1, warmup_steps=1, collate_bucket=32,
        ),
        output_dir=tmp_path,
    )
    assert summary["collate_bucket"] == 32
    assert summary["collate_shapes"]
    assert all(w % 32 == 0 or w == 64 for w in summary["collate_shapes"])
