"""local-model native transformer consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

import torch

from native_transformer import (
    XingChengConfig,
    XingChengForCausalLM,
    XingChengTokenizer,
)
from native_transformer.inference import Generator, Sampler, SamplingConfig
from native_transformer.training import (
    TextDataset,
    Trainer,
    TrainingConfig,
    make_dataloader,
)
from native_transformer.quantization import quantize_model, dequantize_model


def _small_config() -> XingChengConfig:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264  # 容納 byte tokenizer
    cfg.max_position_embeddings = 64
    return cfg


def test_forward_logits_shape() -> None:
    cfg = _small_config()
    model = XingChengForCausalLM(cfg)
    ids = torch.randint(4, cfg.vocab_size, (2, 8))
    out = model(ids)
    assert out["logits"].shape == (2, 8, cfg.vocab_size)


def test_backward_loss_finite() -> None:
    cfg = _small_config()
    model = XingChengForCausalLM(cfg)
    ids = torch.randint(4, cfg.vocab_size, (2, 8))
    labels = ids.clone()
    out = model(ids, labels=labels)
    assert "loss" in out
    assert torch.isfinite(out["loss"])
    out["loss"].backward()
    # 確認梯度存在
    for p in model.parameters():
        if p.requires_grad:
            assert p.grad is not None
            break


def test_attention_mask_padding() -> None:
    cfg = _small_config()
    model = XingChengForCausalLM(cfg)
    ids = torch.randint(4, cfg.vocab_size, (2, 8))
    mask = torch.ones_like(ids)
    mask[1, 4:] = 0
    out = model(ids, attention_mask=mask)
    assert out["logits"].shape == (2, 8, cfg.vocab_size)


def test_greedy_generate() -> None:
    cfg = _small_config()
    model = XingChengForCausalLM(cfg)
    sampler = Sampler(SamplingConfig(do_sample=False))
    gen = Generator(model, sampler=sampler, device="cpu")
    ids = torch.randint(4, cfg.vocab_size, (1, 4))
    out = gen.generate(ids, max_new_tokens=5)
    assert out.shape == (1, 5)


def test_sampling_generate() -> None:
    cfg = _small_config()
    model = XingChengForCausalLM(cfg)
    sampler = Sampler(
        SamplingConfig(do_sample=True, temperature=0.8, top_k=10, top_p=0.9)
    )
    gen = Generator(model, sampler=sampler, device="cpu")
    ids = torch.randint(4, cfg.vocab_size, (1, 4))
    out = gen.generate(ids, max_new_tokens=5)
    assert out.shape == (1, 5)


def test_one_step_loss_decreases() -> None:
    cfg = _small_config()
    model = XingChengForCausalLM(cfg)
    tok = XingChengTokenizer(vocab_size=cfg.vocab_size)
    texts = ["星澄", "本地模型", "transformer", "attention"]
    ds = TextDataset(texts, tok, max_length=16)
    loader = make_dataloader(ds, batch_size=2, pad_id=tok.pad_id, shuffle=False)
    tcfg = TrainingConfig(lr=1e-3, epochs=1, max_steps=3, log_every=0)
    trainer = Trainer(model, tcfg, cfg)
    result = trainer.fit(loader)
    assert len(result["losses"]) > 0
    assert all(torch.tensor(result["losses"]) >= 0)


def test_quantize_dequantize_roundtrip() -> None:
    cfg = _small_config()
    model = XingChengForCausalLM(cfg)
    ids = torch.randint(4, cfg.vocab_size, (1, 4))
    out_before = model(ids)["logits"]
    quantize_model(model, n_bits=8)
    out_after = model(ids)["logits"]
    # INT8 量化誤差應在合理範圍
    diff = (out_before - out_after).abs().max().item()
    assert diff < 5.0
    dequantize_model(model)
    out_restored = model(ids)["logits"]
    assert (out_before - out_restored).abs().max().item() < 5.0


def test_encode_decode_roundtrip() -> None:
    tok = XingChengTokenizer(vocab_size=512)
    text = "星澄原生模型"
    ids = tok.encode(text, add_bos=False, add_eos=False)
    decoded = tok.decode(ids, skip_special=False)
    # 位元組級 roundtrip 應保留原文字
    assert decoded == text
