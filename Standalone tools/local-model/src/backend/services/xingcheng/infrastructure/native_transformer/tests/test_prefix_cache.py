"""Prefix Cache 正式驗收（藍圖 Phase 4 prefix cache 驗收待補項）。

跨請求重用證據、LRU 有界、命中語義、重用後 logits 一致性、
batch>1 不啟用、mask 非全 1 不提交。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

import torch

from native_transformer import XingChengConfig, XingChengForCausalLM
from native_transformer.inference import Generator, PrefixKVStore
from native_transformer.inference.sampler import Sampler, SamplingConfig

torch.manual_seed(17)


def _model() -> XingChengForCausalLM:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    return XingChengForCausalLM(cfg).eval()


def _gen(model, store) -> Generator:
    return Generator(
        model,
        sampler=Sampler(SamplingConfig(do_sample=False, eos_token_id=2, pad_token_id=0)),
        device=torch.device("cpu"),
        prefix_store=store,
    )


def test_cross_request_prefix_reuse() -> None:
    """第一個請求存 prefix K/V；第二個請求共享前綴時命中。"""
    model = _model()
    store = PrefixKVStore(max_entries=4)
    gen = _gen(model, store)
    prompt = torch.randint(1, 264, (1, 16))
    gen.generate(prompt, max_new_tokens=4, use_cache=True)
    assert len(store) == 1
    assert store.hits == 0

    # 第二請求：相同前 16 token + 後綴
    extended = torch.cat([prompt, torch.randint(1, 264, (1, 4))], dim=-1)
    gen.generate(extended, max_new_tokens=4, use_cache=True)
    assert store.hits == 1
    assert gen.last_prefix_reuse == 16  # 整段已存 prefix（16 tok）命中


def test_reused_prefix_produces_identical_logits() -> None:
    """重用 prefix 後的 logits 必須與無快取路徑一致。"""
    model = _model()
    store = PrefixKVStore(max_entries=4)
    prompt = torch.randint(1, 264, (1, 16))

    # 無快取基準
    gen_plain = _gen(model, None)
    ids_ext = torch.cat([prompt, torch.randint(1, 264, (1, 3))], dim=-1)
    ref = gen_plain.generate(ids_ext, max_new_tokens=6, use_cache=True)

    # 先 warm store，再重用
    gen_cached = _gen(model, store)
    gen_cached.generate(prompt, max_new_tokens=4, use_cache=True)
    reused = gen_cached.generate(ids_ext, max_new_tokens=6, use_cache=True)
    assert gen_cached.last_prefix_reuse == 16
    assert torch.equal(ref, reused), "prefix 重用後 greedy 生成必須逐字一致"


def test_lru_eviction_bounded() -> None:
    """超過 max_entries 時最舊項目被逐出。"""
    store = PrefixKVStore(max_entries=3)
    k = torch.zeros(1, 1, 4, 8)
    v = torch.zeros_like(k)
    keys = [store.put(list(range(i, i + 4)), [(k, v)]) for i in range(5)]
    assert len(store) == 3
    # 最早兩筆被逐出
    assert store.longest_prefix_match(list(range(0, 4)))[0] == 0
    assert store.longest_prefix_match(list(range(4, 8)))[0] == 4


def test_no_match_returns_zero() -> None:
    store = PrefixKVStore(max_entries=2)
    k = torch.zeros(1, 1, 4, 8)
    store.put([1, 2, 3], [(k, k)])
    length, kv = store.longest_prefix_match([9, 9, 9])
    assert length == 0 and kv is None
    assert store.misses == 1


def test_batch_gt1_does_not_use_prefix_store() -> None:
    """prefix cache 僅 batch_size=1 啟用（藍圖限制）。"""
    model = _model()
    store = PrefixKVStore(max_entries=4)
    gen = _gen(model, store)
    batch = torch.randint(1, 264, (2, 12))
    gen.generate(batch, max_new_tokens=3, use_cache=True)
    assert gen.last_prefix_reuse == 0
    assert len(store) == 0, "batch>1 不得寫入 prefix store"


def test_masked_prompt_not_stored() -> None:
    """attention_mask 非全 1 的 prompt 不提交入庫（避免污染）。"""
    model = _model()
    store = PrefixKVStore(max_entries=4)
    gen = _gen(model, store)
    ids = torch.randint(1, 264, (1, 10))
    mask = torch.ones_like(ids)
    mask[0, -3:] = 0
    gen.generate(ids, attention_mask=mask, max_new_tokens=3, use_cache=True)
    assert len(store) == 0
