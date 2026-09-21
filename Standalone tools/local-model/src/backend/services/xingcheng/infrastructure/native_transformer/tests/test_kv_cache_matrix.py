"""Phase 5F — KV Cache 正確性與效能矩陣（藍圖 §2.3）。

四情境（短／中／長／最大）× Full Forward vs Prefill＋Decode 逐步 logits 對比；
INT8 量化誤差獨立門檻；效能指標由 `kv_matrix.py` 量測落報告。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

import pytest
import torch

from native_transformer import XingChengConfig, XingChengForCausalLM
from native_transformer.inference import Generator, KVCache

torch.manual_seed(11)

FP_TOL = 1e-5
INT8_TOL = 0.5  # INT8 KV 量化獨立門檻（logits 最大絕對差）


def _cfg() -> XingChengConfig:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    cfg.pad_token_id = 0
    return cfg


def _stepwise_logits(model, ids: torch.Tensor, quant: str | None) -> list[torch.Tensor]:
    """Prefill＋Decode：逐步回傳每步 logits（最後一個位置）。"""
    b, prefix = ids.shape
    device = next(model.parameters()).device
    dtype = next(model.parameters()).dtype
    out = model(ids, use_cache=True)
    logits_seq = [out["logits"][:, -1, :]]
    cache = KVCache(model.config, b, prefix + 4, device, dtype, quant=quant)
    for idx, (k, v) in enumerate(out["kv_caches"]):
        cache.update(idx, k, v, 0)
    for step in range(4):
        pos = prefix + step
        past = cache.slice(pos)
        mask = torch.ones((b, pos + 1), dtype=torch.long, device=device)
        pos_ids = torch.full((b, 1), pos, dtype=torch.long, device=device)
        nxt = torch.full((b, 1), 5 + step, dtype=torch.long, device=device)
        step_out = model(
            nxt, position_ids=pos_ids, attention_mask=mask,
            kv_caches=past, use_cache=True,
        )
        logits_seq.append(step_out["logits"][:, -1, :])
        for idx, (k, v) in enumerate(step_out["kv_caches"]):
            cache.update(idx, k, v, pos)
    return logits_seq


@pytest.mark.parametrize(
    "scenario,prefix_len",
    [("short", 8), ("medium", 32), ("long", 48), ("max", 59)],
)
def test_kv_stepwise_logits_match_full_forward(scenario: str, prefix_len: int) -> None:
    """四情境：prefill+decode 每步 logits 與 full forward 對應位置一致。"""
    cfg = _cfg()
    model = XingChengForCausalLM(cfg).eval()
    ids = torch.randint(1, cfg.vocab_size, (1, prefix_len))
    with torch.no_grad():
        full = model(ids)["logits"][0]  # (S, V)
        steps = _stepwise_logits(model, ids, quant=None)
    # step 0 logits 對應 full[prefix-1]（prefill 末位）
    assert torch.allclose(steps[0][0], full[prefix_len - 1], atol=FP_TOL), scenario
    # 後續 decode 步：同權重同前綴下 logits 應與「把已生成 token 併入後的 full forward」一致
    cur = ids.clone()
    for step, step_logits in enumerate(steps[1:], start=0):
        nxt = torch.full((1, 1), 5 + step, dtype=torch.long)
        cur = torch.cat([cur, nxt], dim=-1)
        with torch.no_grad():
            ref = model(cur)["logits"][0, -1]
        diff = (step_logits[0] - ref).abs().max().item()
        assert diff < FP_TOL, f"{scenario} step{step} diff={diff}"


@pytest.mark.parametrize("prefix_len", [16, 48])
def test_kv_int8_error_within_independent_threshold(prefix_len: int) -> None:
    """INT8 KV cache：量化誤差有獨立門檻，不混入 FP 驗收。"""
    cfg = _cfg()
    model = XingChengForCausalLM(cfg).eval()
    ids = torch.randint(1, cfg.vocab_size, (1, prefix_len))
    with torch.no_grad():
        fp_steps = _stepwise_logits(model, ids, quant=None)
        int8_steps = _stepwise_logits(model, ids, quant="int8")
    diffs = [
        (a[0] - b[0]).abs().max().item() for a, b in zip(fp_steps, int8_steps)
    ]
    assert max(diffs) < INT8_TOL, f"INT8 誤差 {max(diffs)} 超過門檻 {INT8_TOL}"
    # 同時必須真的量化（誤差存在但有限）——量化走 int8 儲存路徑
    cache = KVCache(cfg, 1, 64, torch.device("cpu"), torch.float32, quant="int8")
    assert cache.is_quantized and cache.layers[0][0].dtype == torch.int8


def test_kv_context_overflow_is_bounded() -> None:
    """Context Overflow：超過 max_position_embeddings 必須明確失敗。"""
    cfg = _cfg()
    model = XingChengForCausalLM(cfg).eval()
    gen = Generator(model)
    ids = torch.randint(1, cfg.vocab_size, (1, cfg.max_position_embeddings - 2))
    with pytest.raises(ValueError, match="SEQUENCE_EXCEEDS"):
        gen.generate(ids, max_new_tokens=8)


def test_kv_cache_memory_layout_preallocated() -> None:
    """Cache Position／Length／Update：預配置上限、slice 語義、reset 清空。"""
    cfg = _cfg()
    cache = KVCache(cfg, 1, 64, torch.device("cpu"), torch.float32)
    k, v = cache[0]
    assert k.shape == (1, cfg.num_key_value_heads, 64, cfg.head_dim)
    new_k = torch.randn(1, cfg.num_key_value_heads, 3, cfg.head_dim)
    new_v = torch.randn_like(new_k)
    cache.update(0, new_k, new_v, 5)
    past = cache.slice(8)
    assert past[0][0].shape[2] == 8
    assert torch.allclose(past[0][0][:, :, 5:8, :], new_k)
    cache.reset()
    assert torch.count_nonzero(cache.layers[0][0]) == 0
