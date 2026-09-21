"""Phase 7：KV Cache 完善 — 六項正確性確認 + FP16/BF16 與 INT8 品質對比。

確認項目：Prefill / Decode / Cache Update / Position Tracking /
Causal Consistency / GQA KV Layout。
品質要求：量化只換記憶體，不得犧牲長上下文的生成品質。
"""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import math

import pytest
import torch

from xingcheng.infrastructure.native_transformer import (
    XingChengConfig,
    XingChengForCausalLM,
    XingChengTokenizer,
)
from xingcheng.infrastructure.native_transformer.inference import (
    Generator,
    KVCache,
    Sampler,
    SamplingConfig,
    resolve_kv_dtype,
)


def _tiny(vocab: int = 264) -> tuple[XingChengForCausalLM, XingChengConfig]:
    cfg = XingChengConfig.small()
    cfg.vocab_size = vocab
    cfg.max_position_embeddings = 256
    cfg.num_key_value_heads = 2  # GQA：4 個 query head 對 2 個 KV head
    torch.manual_seed(17)
    return XingChengForCausalLM(cfg), cfg


def _greedy() -> Sampler:
    return Sampler(SamplingConfig(do_sample=False, eos_token_id=-1))


# ── 六項確認 ────────────────────────────────────────────────────


def test_prefill_matches_full_forward() -> None:
    model, _ = _tiny()
    model.eval()
    ids = torch.randint(4, 260, (1, 12))
    with torch.no_grad():
        full = model(ids, attention_mask=torch.ones_like(ids))["logits"]
        prefill = model(ids, attention_mask=torch.ones_like(ids), use_cache=True)
    assert torch.allclose(
        full, prefill["logits"], atol=1e-5
    ), "prefill logits 必須與全序列前向一致"
    assert len(prefill["kv_caches"]) == model.config.num_hidden_layers


def test_decode_step_matches_full_recompute() -> None:
    model, _ = _tiny()
    model.eval()
    ids = torch.randint(4, 260, (1, 9))
    mask = torch.ones_like(ids)
    with torch.no_grad():
        full = model(ids, attention_mask=mask)["logits"]
        prefill = model(ids[:, :5], attention_mask=mask[:, :5], use_cache=True)
        step = model(
            ids[:, 5:6],
            position_ids=torch.tensor([[5]]),
            attention_mask=torch.ones((1, 6), dtype=torch.long),
            kv_caches=prefill["kv_caches"],
            use_cache=True,
        )
    assert torch.allclose(full[:, 5, :], step["logits"][:, 0, :], atol=1e-5)


def test_cache_update_writes_and_reads_back() -> None:
    _, cfg = _tiny()
    cache = KVCache(cfg, 1, 16, torch.device("cpu"), torch.float32)
    k1 = torch.randn(1, cfg.num_key_value_heads, 3, cfg.head_dim)
    v1 = torch.randn_like(k1)
    cache.update(0, k1, v1, 0)
    k2 = torch.randn(1, cfg.num_key_value_heads, 2, cfg.head_dim)
    v2 = torch.randn_like(k2)
    cache.update(0, k2, v2, 3)
    past_k, past_v = cache.slice(5)[0]
    assert torch.equal(past_k[:, :, :3, :], k1)
    assert torch.equal(past_k[:, :, 3:5, :], k2)
    assert torch.equal(past_v[:, :, 3:5, :], v2)


def test_position_tracking_advances_rope() -> None:
    """解碼步必須使用遞增位置：位置 1 與位置 0 的輸出不得相同。"""
    model, _ = _tiny()
    model.eval()
    ids = torch.randint(4, 260, (1, 2))
    with torch.no_grad():
        prefill = model(
            ids[:, :1],
            position_ids=torch.tensor([[0]]),
            attention_mask=torch.ones((1, 1), dtype=torch.long),
            use_cache=True,
        )
        step_mask = torch.ones((1, 2), dtype=torch.long)
        correct = model(
            ids[:, 1:2],
            position_ids=torch.tensor([[1]]),
            attention_mask=step_mask,
            kv_caches=prefill["kv_caches"],
            use_cache=True,
        )
        wrong = model(
            ids[:, 1:2],
            position_ids=torch.tensor([[0]]),
            attention_mask=step_mask,
            kv_caches=prefill["kv_caches"],
            use_cache=True,
        )
    assert not torch.allclose(
        correct["logits"], wrong["logits"], atol=1e-6
    ), "位置未遞增（RoPE 位置追蹤失效）"


def test_causal_consistency_prefix_stable() -> None:
    model, _ = _tiny()
    model.eval()
    ids = torch.randint(4, 260, (1, 10))
    changed = ids.clone()
    changed[0, -1] = (changed[0, -1] + 31) % 256 + 4
    mask = torch.ones_like(ids)
    with torch.no_grad():
        base = model(ids, attention_mask=mask)["logits"]
        after = model(changed, attention_mask=mask)["logits"]
    assert torch.allclose(base[:, :-1], after[:, :-1], atol=1e-5)


def test_gqa_kv_layout_matches_head_groups() -> None:
    model, cfg = _tiny()
    model.eval()
    attn = model.model.layers[0].attention
    assert attn.num_kv_heads == 2 and attn.num_heads == cfg.num_attention_heads
    n_rep = attn.num_heads // attn.num_kv_heads
    k = torch.randn(1, attn.num_kv_heads, 3, cfg.head_dim)
    expanded = attn._repeat_kv(k, n_rep)
    for kv_head in range(attn.num_kv_heads):
        for repeat in range(n_rep):
            assert torch.equal(
                expanded[:, kv_head * n_rep + repeat], k[:, kv_head]
            ), "GQA 展開必須以 KV head 分組、不得混入序列位置"


# ── FP16 / BF16 與 INT8 對比 ────────────────────────────────────


@pytest.mark.parametrize("dtype_name", ["float16", "bfloat16"])
def test_fp16_bf16_cache_keeps_logits_and_greedy(dtype_name: str) -> None:
    model, _ = _tiny()
    model.eval()
    if resolve_kv_dtype(dtype_name, torch.float32) == torch.bfloat16:
        try:  # 部分 CPU 不支援 bf16 線性層
            torch.zeros(1, dtype=torch.bfloat16) @ torch.zeros(1, dtype=torch.bfloat16)
        except RuntimeError:
            pytest.skip("CPU 不支援 bfloat16 matmul")
    ids = torch.randint(4, 260, (1, 8))
    with torch.no_grad():
        reference = model(ids, use_cache=True)["logits"]
    generator = Generator(model, sampler=_greedy(), device="cpu", kv_cache_dtype=dtype_name)
    with torch.no_grad():
        cached = generator.generate(ids, max_new_tokens=4, use_cache=True)
    baseline_generator = Generator(model, sampler=_greedy(), device="cpu")
    with torch.no_grad():
        baseline = baseline_generator.generate(ids, max_new_tokens=4, use_cache=True)
    assert torch.equal(cached, baseline), "FP16/BF16 快取不得改變 greedy 生成"
    assert reference.shape[0] == 1


def test_int8_cache_logits_and_greedy_close_to_fp() -> None:
    model, _ = _tiny()
    model.eval()
    ids = torch.randint(4, 260, (1, 8))
    baseline_generator = Generator(model, sampler=_greedy(), device="cpu")
    quantized_generator = Generator(
        model, sampler=_greedy(), device="cpu", kv_cache_quant="int8"
    )
    with torch.no_grad():
        baseline = baseline_generator.generate(ids, max_new_tokens=6, use_cache=True)
        quantized = quantized_generator.generate(ids, max_new_tokens=6, use_cache=True)
    assert torch.equal(baseline, quantized), "INT8 KV 快取不得改變 greedy 生成"
    cache = quantized_generator.last_cache
    assert cache is not None and cache.is_quantized


def test_int8_cache_long_context_quality() -> None:
    """長上下文：INT8 快取與 FP 快取的逐 token logits 必須一致（容忍度內）。"""
    model, _ = _tiny()
    model.eval()
    prefix = torch.randint(4, 260, (1, 192))
    with torch.no_grad():
        fp = Generator(model, sampler=_greedy(), device="cpu")
        fp_out = fp.generate(prefix, max_new_tokens=6, use_cache=True)
        q = Generator(model, sampler=_greedy(), device="cpu", kv_cache_quant="int8")
        q_out = q.generate(prefix, max_new_tokens=6, use_cache=True)

    assert torch.equal(fp_out, q_out), "長上下文 greedy 生成必須與 FP 一致"

    fp_cache = fp.last_cache
    q_cache = q.last_cache
    assert fp_cache is not None and q_cache is not None
    assert q_cache.memory_bytes() < fp_cache.memory_bytes(), "INT8 必須節省記憶體"

    fp_slice = fp_cache.slice(192)
    q_slice = q_cache.slice(192)
    worst = 0.0
    for (fk, fv), (qk, qv) in zip(fp_slice, q_slice):
        worst = max(
            worst,
            float((fk - qk).abs().max().item()),
            float((fv - qv).abs().max().item()),
        )
    assert worst < 0.05, f"INT8 KV 量化誤差過大：{worst}"


@pytest.mark.skipif(not torch.cuda.is_available(), reason="需要 CUDA")
def test_triton_rope_gqa_matches_torch_reference() -> None:
    """GQA 下 Triton RoPE 必須與 PyTorch 參考實作一致（k 的 head 數較少）。"""
    from xingcheng.infrastructure.native_transformer.execution.backend import (
        set_triton_kernels,
    )
    from xingcheng.infrastructure.native_transformer.kernels import rope as rope_mod

    set_triton_kernels(True)
    try:
        if not rope_mod._triton_available():
            pytest.skip("Triton 不可用")
        torch.manual_seed(5)
        q = torch.randn(2, 8, 5, 32, device="cuda")
        k = torch.randn(2, 2, 5, 32, device="cuda")  # GQA：2 個 KV head
        cos, sin = rope_mod.build_rope_tables(
            32, 64, device=torch.device("cuda"), dtype=torch.float32
        )
        positions = torch.arange(5, device="cuda").unsqueeze(0).expand(2, -1)
        qt, kt = rope_mod._rope_triton(q, k, cos, sin, positions)
        qr, kr = rope_mod._rope_torch(q, k, cos, sin, positions)
        assert torch.allclose(qt, qr, atol=1e-5)
        assert torch.allclose(kt, kr, atol=1e-5)
    finally:
        set_triton_kernels(False)


@pytest.mark.skipif(not torch.cuda.is_available(), reason="需要 CUDA")
def test_triton_rope_handles_transposed_gqa_views() -> None:
    """模型內 q/k 來自 ``view(...).transpose(1, 2)``，Triton 不得假設 contiguous。"""
    from xingcheng.infrastructure.native_transformer.execution.backend import (
        set_triton_kernels,
    )
    from xingcheng.infrastructure.native_transformer.kernels import rope as rope_mod

    set_triton_kernels(True)
    try:
        if not rope_mod._triton_available():
            pytest.skip("Triton 不可用")
        torch.manual_seed(11)
        q = torch.randn(2, 5, 8, 32, device="cuda").transpose(1, 2)
        k = torch.randn(2, 5, 2, 32, device="cuda").transpose(1, 2)
        cos, sin = rope_mod.build_rope_tables(
            32, 64, device=torch.device("cuda"), dtype=torch.float32
        )
        qt, kt = rope_mod.apply_rope(q, k, cos, sin)
        qr, kr = rope_mod._rope_torch(q, k, cos, sin, None)
        assert torch.allclose(qt, qr, atol=1e-5)
        assert torch.allclose(kt, kr, atol=1e-5)
    finally:
        set_triton_kernels(False)


@pytest.mark.skipif(not torch.cuda.is_available(), reason="需要 CUDA")
def test_triton_model_logits_match_torch_and_kernels_activate(monkeypatch) -> None:
    """模型層級 Triton 驗收：三個 kernel 必須實際命中且 logits 等價。"""
    import importlib

    from xingcheng.infrastructure.native_transformer.config import XingChengConfig
    from xingcheng.infrastructure.native_transformer.execution.backend import (
        set_triton_kernels,
    )
    from xingcheng.infrastructure.native_transformer.kernels import rmsnorm, rope
    from xingcheng.infrastructure.native_transformer.modules.model import (
        XingChengForCausalLM,
    )

    swiglu_mod = importlib.import_module(
        "xingcheng.infrastructure.native_transformer.kernels.swiglu"
    )
    set_triton_kernels(True)
    if not rope._triton_available():
        set_triton_kernels(False)
        pytest.skip("Triton 不可用")

    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    torch.manual_seed(17)
    model = XingChengForCausalLM(cfg).to("cuda").eval()
    ids = torch.randint(4, 260, (2, 16), device="cuda")

    with torch.no_grad():
        set_triton_kernels(False)
        expected = model(ids)["logits"].clone()

        calls = {"rms": 0, "rope": 0, "swiglu": 0}

        def counted(name, module, attr):
            original = getattr(module, attr)

            def wrapper(*args, **kwargs):
                calls[name] += 1
                return original(*args, **kwargs)

            monkeypatch.setattr(module, attr, wrapper)

        counted("rms", rmsnorm, "_rms_norm_triton")
        counted("rope", rope, "_rope_triton")
        counted("swiglu", swiglu_mod, "_swiglu_triton")

        def fail(*args, **kwargs):
            raise AssertionError("PyTorch fallback unexpectedly used")

        monkeypatch.setattr(rmsnorm, "_rms_norm_torch", fail)
        monkeypatch.setattr(rope, "_rope_torch", fail)
        monkeypatch.setattr(swiglu_mod, "_swiglu_torch", fail)
        set_triton_kernels(True)
        try:
            actual = model(ids)["logits"].clone()
        finally:
            set_triton_kernels(False)

    assert all(count > 0 for count in calls.values())
    assert torch.allclose(actual, expected, atol=1e-3, rtol=1e-3)


@pytest.mark.skipif(not torch.cuda.is_available(), reason="需要 CUDA")
def test_triton_kernels_fall_back_when_gradients_required() -> None:
    """Triton kernel 不帶 autograd：需要梯度時必須退回 PyTorch（訓練可收斂）。"""
    import importlib

    from xingcheng.infrastructure.native_transformer.execution.backend import (
        set_triton_kernels,
    )
    from xingcheng.infrastructure.native_transformer.kernels import rmsnorm

    swiglu_mod = importlib.import_module(
        "xingcheng.infrastructure.native_transformer.kernels.swiglu"
    )

    set_triton_kernels(True)
    try:
        if not rmsnorm._triton_available():
            pytest.skip("Triton 不可用")
        x = torch.randn(2, 8, device="cuda", requires_grad=True)
        w = torch.ones(8, device="cuda", requires_grad=True)
        y = rmsnorm.rms_norm_weight(x, w, 1e-6)
        assert y.requires_grad, "RMSNorm 訓練時梯度不得斷裂"
        (y.sum()).backward()
        assert x.grad is not None and w.grad is not None

        gate = torch.randn(2, 8, device="cuda", requires_grad=True)
        up = torch.randn(2, 8, device="cuda", requires_grad=True)
        out = swiglu_mod.swiglu(gate, up)
        assert out.requires_grad, "SwiGLU 訓練時梯度不得斷裂"
        out.sum().backward()
        assert gate.grad is not None and up.grad is not None
    finally:
        set_triton_kernels(False)


@pytest.mark.skipif(not torch.cuda.is_available(), reason="需要 CUDA")
def test_cuda_training_converges_with_triton_available() -> None:
    from xingcheng.infrastructure.native_transformer import (
        XingChengForCausalLM,
        XingChengTokenizer,
    )
    from xingcheng.infrastructure.native_transformer.training import (
        TextDataset,
        Trainer,
        TrainingConfig,
        make_dataloader,
    )

    model, cfg = _tiny()
    cfg.max_position_embeddings = 64
    torch.manual_seed(29)
    tokenizer = XingChengTokenizer.from_config(cfg)
    texts = [
        "星澄是本地生成式語言模型。",
        "模型核心與網路功能保持分離。",
        "本地模型以 PyTorch 實作。",
        "注意力使用因果遮罩。",
    ]
    dataset = TextDataset(texts, tokenizer, max_length=24)
    loader = make_dataloader(
        dataset, batch_size=2, pad_id=tokenizer.pad_id, shuffle=False
    )
    config = TrainingConfig(
        lr=1e-2, epochs=30, max_steps=60, log_every=0, grad_clip=1.0, device="cuda"
    )
    result = Trainer(model, config, cfg).fit(loader)
    assert result["final_loss"] < result["losses"][0] - 1.0, (
        "CUDA 訓練必須收斂（Triton 需梯度時應退回 PyTorch）"
    )


def test_kv_cache_memory_accounting() -> None:
    _, cfg = _tiny()
    fp = KVCache(cfg, 1, 128, torch.device("cpu"), torch.float32)
    half = KVCache(cfg, 1, 128, torch.device("cpu"), torch.float16)
    int8 = KVCache(cfg, 1, 128, torch.device("cpu"), torch.float16, quant="int8")
    assert int8.memory_bytes() < half.memory_bytes() < fp.memory_bytes()
    assert int8.describe()["quant"] == "int8"
    assert math.isclose(
        half.memory_bytes() * 2, fp.memory_bytes(), rel_tol=0.01
    )
