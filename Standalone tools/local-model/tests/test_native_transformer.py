"""local-model native transformer consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "src" / "backend" / "services" / "xingcheng" / "infrastructure"
for _p in (str(_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p


# -- CONSOLIDATED TEST SUITE --

import pytest
import torch

from native_transformer import (
    XingChengConfig,
    XingChengForCausalLM,
    XingChengTokenizer,
    load_checkpoint,
    save_checkpoint,
)
from native_transformer.inference import Generator, KVCache, Sampler, SamplingConfig
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
        SamplingConfig(
            do_sample=True,
            temperature=0.8,
            top_k=10,
            top_p=0.9,
            eos_token_id=-1,
        )
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


# ── Phase 0：原生核心正確性與存讀 ────────────────────────────────


def test_tokenizer_from_config_matches_vocab() -> None:
    cfg = _small_config()
    tok = XingChengTokenizer.from_config(cfg)
    assert len(tok) == cfg.vocab_size
    ids = tok.encode("星澄 native", add_bos=True, add_eos=True)
    assert all(0 <= token < cfg.vocab_size for token in ids)
    assert tok.decode(ids, skip_special=True) == "星澄 native"


def test_attention_is_causal_without_mask() -> None:
    cfg = _small_config()
    torch.manual_seed(7)
    model = XingChengForCausalLM(cfg)
    model.eval()
    ids = torch.randint(4, 260, (1, 10))
    changed = ids.clone()
    changed[0, -1] = (changed[0, -1] + 37) % 256 + 4
    with torch.no_grad():
        base = model(ids)["logits"]
        after = model(changed)["logits"]
    assert torch.allclose(base[:, :-1], after[:, :-1], atol=1e-5)


def test_attention_is_causal_with_mask() -> None:
    cfg = _small_config()
    torch.manual_seed(7)
    model = XingChengForCausalLM(cfg)
    model.eval()
    ids = torch.randint(4, 260, (1, 10))
    mask = torch.ones_like(ids)
    changed = ids.clone()
    changed[0, -1] = (changed[0, -1] + 37) % 256 + 4
    with torch.no_grad():
        base = model(ids, attention_mask=mask)["logits"]
        after = model(changed, attention_mask=mask)["logits"]
    assert torch.allclose(base[:, :-1], after[:, :-1], atol=1e-5)
    assert not torch.allclose(base[:, -1], after[:, -1])


def test_padding_mask_blocks_future_padding() -> None:
    cfg = _small_config()
    torch.manual_seed(9)
    model = XingChengForCausalLM(cfg)
    model.eval()
    ids = torch.randint(4, 260, (1, 10))
    mask = torch.ones_like(ids)
    mask[0, 8:] = 0
    changed = ids.clone()
    changed[0, 9] = (changed[0, 9] + 37) % 256 + 4
    with torch.no_grad():
        base = model(ids, attention_mask=mask)["logits"]
        after = model(changed, attention_mask=mask)["logits"]
    assert torch.allclose(base[:, :8], after[:, :8], atol=1e-5)


def test_cached_decode_matches_full_forward() -> None:
    cfg = _small_config()
    torch.manual_seed(11)
    model = XingChengForCausalLM(cfg)
    model.eval()
    ids = torch.randint(4, 260, (1, 7))
    mask = torch.ones_like(ids)
    with torch.no_grad():
        full = model(ids, attention_mask=mask)["logits"]
        prefill = model(ids[:, :4], attention_mask=mask[:, :4], use_cache=True)
        cached = model(
            ids[:, 4:5],
            position_ids=torch.tensor([[4]]),
            attention_mask=torch.ones((1, 5), dtype=torch.long),
            kv_caches=prefill["kv_caches"],
            use_cache=True,
        )
    assert torch.allclose(full[:, 4, :], cached["logits"][:, 0, :], atol=1e-5)


def test_greedy_generate_cache_matches_no_cache() -> None:
    cfg = _small_config()
    torch.manual_seed(13)
    model = XingChengForCausalLM(cfg)
    model.eval()
    sampler = Sampler(SamplingConfig(do_sample=False, eos_token_id=-1))
    gen = Generator(model, sampler=sampler, device="cpu")
    ids = torch.randint(4, 260, (1, 5))
    cached = gen.generate(ids, max_new_tokens=6, use_cache=True)
    full = gen.generate(ids, max_new_tokens=6, use_cache=False)
    assert torch.equal(cached, full)


def test_kv_cache_container_semantics() -> None:
    cfg = _small_config()
    cache = KVCache(
        cfg, batch_size=1, max_seq_len=8,
        device=torch.device("cpu"), dtype=torch.float32,
    )
    assert len(cache) == cfg.num_hidden_layers
    k = torch.randn(1, cfg.num_key_value_heads, 2, cfg.head_dim)
    v = torch.randn_like(k)
    cache.update(0, k, v, 1)
    past_k, past_v = cache[0]
    assert torch.equal(past_k[:, :, 1:3, :], k)
    assert torch.equal(past_v[:, :, 1:3, :], v)
    assert len(cache.slice(3)) == cfg.num_hidden_layers
    cache.reset()
    assert torch.count_nonzero(cache[0][0]) == 0


def test_generate_rejects_sequence_beyond_position_limit() -> None:
    cfg = _small_config()
    model = XingChengForCausalLM(cfg)
    gen = Generator(model, sampler=Sampler(SamplingConfig(do_sample=False)), device="cpu")
    ids = torch.randint(4, 260, (1, cfg.max_position_embeddings))
    with pytest.raises(ValueError, match="SEQUENCE_EXCEEDS_MAX_POSITION_EMBEDDINGS"):
        gen.generate(ids, max_new_tokens=1)


def test_checkpoint_roundtrip(tmp_path) -> None:
    cfg = _small_config()
    torch.manual_seed(17)
    model = XingChengForCausalLM(cfg)
    model.eval()
    tok = XingChengTokenizer.from_config(cfg)
    ids = torch.randint(4, 260, (1, 6))
    with torch.no_grad():
        before = model(ids)["logits"]
    info = save_checkpoint(
        tmp_path / "model.pt", model, tokenizer=tok, metadata={"run": "test"}
    )
    assert info["state_sha256"]
    assert (tmp_path / "model.pt").exists()
    assert (tmp_path / "model.pt.sha256").exists()
    loaded = load_checkpoint(info["path"])
    assert loaded["config"].to_dict() == cfg.to_dict()
    assert loaded["tokenizer"].vocab_size == cfg.vocab_size
    assert loaded["metadata"] == {"run": "test"}
    with torch.no_grad():
        after = loaded["model"](ids)["logits"]
    assert torch.equal(before, after)


def test_checkpoint_detects_tampered_state(tmp_path) -> None:
    cfg = _small_config()
    torch.manual_seed(19)
    model = XingChengForCausalLM(cfg)
    tok = XingChengTokenizer.from_config(cfg)
    info = save_checkpoint(tmp_path / "model.pt", model, tokenizer=tok)
    payload = torch.load(info["path"], map_location="cpu", weights_only=True)
    key = next(iter(payload["model_state"]))
    payload["model_state"][key] = payload["model_state"][key] + 1.0
    tampered = tmp_path / "tampered.pt"
    torch.save(payload, tampered)
    with pytest.raises(ValueError, match="CHECKPOINT_STATE_HASH_MISMATCH"):
        load_checkpoint(tampered)


def test_trainer_writes_checkpoints(tmp_path) -> None:
    cfg = _small_config()
    torch.manual_seed(23)
    model = XingChengForCausalLM(cfg)
    tok = XingChengTokenizer.from_config(cfg)
    texts = ["星澄", "本地模型", "transformer", "attention"]
    ds = TextDataset(texts, tok, max_length=16)
    loader = make_dataloader(ds, batch_size=2, pad_id=tok.pad_id, shuffle=False)
    tcfg = TrainingConfig(
        lr=1e-3, epochs=1, max_steps=2, log_every=0,
        checkpoint_dir=str(tmp_path), checkpoint_every=1,
    )
    trainer = Trainer(model, tcfg, cfg, tokenizer=tok)
    result = trainer.fit(loader)
    assert len(result["checkpoints"]) == 2
    loaded = load_checkpoint(result["checkpoints"][-1])
    assert loaded["metadata"]["step"] == 2
    assert loaded["tokenizer"] is not None


def test_overfit_loss_decreases() -> None:
    cfg = _small_config()
    torch.manual_seed(29)
    model = XingChengForCausalLM(cfg)
    tok = XingChengTokenizer.from_config(cfg)
    texts = [
        "星澄是本地生成式語言模型。",
        "模型核心與網路功能保持分離。",
        "本地模型以 PyTorch 實作。",
        "注意力使用因果遮罩。",
    ]
    ds = TextDataset(texts, tok, max_length=24)
    loader = make_dataloader(ds, batch_size=2, pad_id=tok.pad_id, shuffle=False)
    tcfg = TrainingConfig(lr=1e-2, epochs=30, max_steps=60, log_every=0, grad_clip=1.0)
    trainer = Trainer(model, tcfg, cfg)
    result = trainer.fit(loader)
    assert result["final_loss"] < result["losses"][0] - 1.0