# -*- coding: utf-8 -*-
"""Prefix KV 重用、多輪 ChatSession、executor 生命週期簿記。"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
import torch

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "src"
        / "backend"
        / "services"
        / "xingcheng"
        / "infrastructure"
    ),
)

from native_transformer.config import XingChengConfig  # noqa: E402
from native_transformer.inference import (  # noqa: E402
    ChatSession,
    Generator,
    PrefixKVStore,
    Sampler,
    SamplingConfig,
)
from native_transformer.modules.model import XingChengForCausalLM  # noqa: E402
from native_transformer.tokenizer import XingChengTokenizer  # noqa: E402


def _tiny_model():
    cfg = XingChengConfig.small()
    cfg.vocab_size = 300
    cfg.max_position_embeddings = 128
    torch.manual_seed(7)
    return XingChengForCausalLM(cfg).eval()


# ── PrefixKVStore ─────────────────────────────────────────────
def test_store_longest_prefix_match():
    store = PrefixKVStore(max_entries=4, tag="t")
    k = torch.zeros(1, 2, 3, 4)
    v = torch.zeros(1, 2, 3, 4)
    store.put([1, 2, 3], [(k, v)])
    store.put([1, 2, 3, 4, 5], [(k, v)])
    length, kv = store.longest_prefix_match([1, 2, 3, 4, 5, 9])
    assert length == 5 and kv is not None
    length, kv = store.longest_prefix_match([1, 2, 9])
    assert length == 0 and kv is None  # 部分前綴不算命中（entry 需整條命中）
    length, kv = store.longest_prefix_match([9, 9])
    assert length == 0
    assert store.stats()["hits"] == 1


def test_store_lru_eviction():
    store = PrefixKVStore(max_entries=2)
    kv = [(torch.zeros(1, 1, 1, 1), torch.zeros(1, 1, 1, 1))]
    store.put([1], kv)
    store.put([2], kv)
    store.put([3], kv)  # 擠掉 [1]
    assert store.longest_prefix_match([1, 9])[0] == 0
    assert store.longest_prefix_match([3, 9])[0] == 1


# ── Generator prefix reuse ────────────────────────────────────
def test_generator_prefix_reuse_identical_output():
    model = _tiny_model()
    tok = XingChengTokenizer(vocab_size=300)
    prompt = torch.tensor([tok.encode("星澄原生模型前綴")], dtype=torch.long)
    prefix_len = prompt.size(1)

    plain = Generator(model, sampler=Sampler(SamplingConfig(do_sample=False)), device="cpu")
    out_plain = plain.generate(prompt, max_new_tokens=6)

    store = PrefixKVStore(max_entries=4)
    cached = Generator(
        model, sampler=Sampler(SamplingConfig(do_sample=False)), prefix_store=store,
        device="cpu",
    )
    first = cached.generate(prompt.clone(), max_new_tokens=6)
    assert cached.last_prefix_reuse == 0  # 首次無命中
    second = cached.generate(prompt.clone(), max_new_tokens=6)
    assert cached.last_prefix_reuse == prefix_len - 1  # 整段命中
    assert torch.equal(first, out_plain)
    assert torch.equal(second, out_plain)


def test_generator_prefix_partial_reuse():
    model = _tiny_model()
    tok = XingChengTokenizer(vocab_size=300)
    store = PrefixKVStore(max_entries=4)
    gen = Generator(
        model, sampler=Sampler(SamplingConfig(do_sample=False)), prefix_store=store,
        device="cpu",
    )
    base = torch.tensor([tok.encode("共同前綴")], dtype=torch.long)
    gen.generate(base, max_new_tokens=4)
    shared = torch.tensor([tok.encode("共同前綴尾巴")], dtype=torch.long)
    gen.generate(shared, max_new_tokens=4)
    assert gen.last_prefix_reuse >= 1  # 命中共同前綴
    # 不同前綴不命中
    other = torch.tensor([tok.encode("完全不同")], dtype=torch.long)
    gen.generate(other, max_new_tokens=4)
    assert gen.last_prefix_reuse == 0


def test_generator_prefix_batch_skipped():
    model = _tiny_model()
    tok = XingChengTokenizer(vocab_size=300)
    store = PrefixKVStore()
    gen = Generator(model, prefix_store=store, device="cpu")
    ids = torch.tensor(
        [tok.encode("A"), tok.encode("B")], dtype=torch.long
    )
    gen.generate(ids, max_new_tokens=3)
    assert gen.last_prefix_reuse == 0  # batch>1 不走 prefix cache


# ── ChatSession ───────────────────────────────────────────────
def test_chat_session_roundtrip_and_tool_call():
    model = _tiny_model()
    tok = XingChengTokenizer(vocab_size=300)
    gen = Generator(model, sampler=Sampler(SamplingConfig(do_sample=False)), device="cpu")
    session = ChatSession(gen, tok, system_prompt="你是星澄", max_context=96)
    reply = session.step("你好", max_new_tokens=4)
    assert reply.generated_tokens == 4
    assert session.messages[0].role == "system"
    assert session.messages[1].role == "user"
    assert session.messages[-1].role == "assistant"
    session.add_tool_result("工具結果", name="search")
    assert session.messages[-1].role == "tool"
    assert "search" in session.messages[-1].content


def test_chat_session_prompt_within_context():
    model = _tiny_model()
    tok = XingChengTokenizer(vocab_size=300)
    gen = Generator(model, device="cpu")
    session = ChatSession(gen, tok, max_context=48)
    for i in range(6):
        session.add_user_message("長內容 " * 10)
        session.messages.append(
            type(session.messages[-1])("assistant", "回覆")
        )
    ids = session.prompt_ids(reserve=8)
    assert ids.size(1) <= 40
    assert int(ids[0, 0]) == tok.bos_id


# ── Executor 生命週期簿記 ─────────────────────────────────────
def test_executor_records_lifecycle(tmp_path: Path):
    tests_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(tests_dir))
    import _xingcheng_test_support  # noqa: F401
    from test_training_job_executor import _fake_train_fn, _queued_job
    from xingcheng.infrastructure.training_job_executor import TrainingJobExecutor
    from xingcheng.infrastructure.transformer_training_repository import (
        TransformerTrainingRepository,
    )
    from xingcheng.infrastructure.native_transformer.lifecycle import (
        ModelLifecycle,
    )

    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(repository, tmp_path, max_steps=4)
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)
    result = executor.run_job(str(job["job_id"]))
    assert result["ok"] is True

    lc_dir = executor.tool_root / "runtime" / "models" / "lifecycle" / "xingcheng-native"
    lifecycle = ModelLifecycle.load(lc_dir)
    assert lifecycle.state == "PRETRAINED"
    weights = lifecycle.active_weights()
    assert weights is not None
    assert weights["metadata"]["job_id"] == str(job["job_id"])
    assert Path(weights["path"]).name == "final.pt"


def test_executor_lifecycle_marks_failure(tmp_path: Path):
    tests_dir = Path(__file__).resolve().parent
    sys.path.insert(0, str(tests_dir))
    import _xingcheng_test_support  # noqa: F401
    from test_training_job_executor import _queued_job
    from xingcheng.infrastructure.training_job_executor import TrainingJobExecutor
    from xingcheng.infrastructure.transformer_training_repository import (
        TransformerTrainingRepository,
    )
    from xingcheng.infrastructure.native_transformer.lifecycle import (
        ModelLifecycle,
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("loss exploded")

    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(repository, tmp_path)
    executor = TrainingJobExecutor(repository, train_fn=_boom)
    result = executor.run_job(str(job["job_id"]))
    assert result["ok"] is False

    lc_dir = executor.tool_root / "runtime" / "models" / "lifecycle" / "xingcheng-native"
    lifecycle = ModelLifecycle.load(lc_dir)
    assert lifecycle.state == "FAILED"
    assert lifecycle.history[-1]["to"] == "FAILED"
