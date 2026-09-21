"""DPO preference training: loss semantics, smoke run, resume state."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import math
from pathlib import Path

import pytest
import torch

from xingcheng.infrastructure.native_transformer import (
    XingChengConfig,
    XingChengForCausalLM,
    XingChengTokenizer,
    load_checkpoint,
)
from xingcheng.infrastructure.native_transformer.training import (
    DpoConfig,
    dpo_loss,
    dpo_train,
    dpo_train_from_repository,
    load_preference_pairs,
)


def _tiny() -> tuple[XingChengForCausalLM, XingChengTokenizer]:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    torch.manual_seed(11)
    return XingChengForCausalLM(cfg), XingChengTokenizer.from_config(cfg)


def _pairs() -> list[dict[str, str]]:
    return [
        {
            "prompt_text": "什麼是遞迴？",
            "chosen_text": "遞迴是函式呼叫自身的技巧。",
            "rejected_text": "不知道。",
        },
        {
            "prompt_text": "1+1 是多少？",
            "chosen_text": "答案是 2。",
            "rejected_text": "大概是 3。",
        },
        {
            "prompt_text": "列出水果",
            "chosen_text": "蘋果、香蕉、橘子。",
            "rejected_text": "蘋果蘋果蘋果蘋果。",
        },
        {
            "prompt_text": "說明排序",
            "chosen_text": "排序是將元素依大小排列。",
            "rejected_text": "排序很難。",
        },
    ]


def test_dpo_loss_rewards_chosen_preference() -> None:
    preferred = dpo_loss(
        torch.tensor([2.0]), torch.tensor([0.5]),
        torch.tensor([0.0]), torch.tensor([0.0]), beta=0.5,
    )
    discouraged = dpo_loss(
        torch.tensor([0.5]), torch.tensor([2.0]),
        torch.tensor([0.0]), torch.tensor([0.0]), beta=0.5,
    )
    loss_ok, acc_ok = preferred
    loss_bad, acc_bad = discouraged
    assert loss_ok.item() < math.log(2)
    assert acc_ok.item() == 1.0
    assert loss_bad.item() > math.log(2)
    assert acc_bad.item() == 0.0


def test_dpo_train_smoke_produces_resumable_checkpoint(tmp_path) -> None:
    model, tokenizer = _tiny()
    summary = dpo_train(
        model,
        tokenizer,
        _pairs(),
        DpoConfig(max_steps=4, batch_size=2, log_every=0, checkpoint_every=0),
        output_dir=tmp_path / "dpo",
    )

    assert summary["phase"] == "dpo"
    assert summary["steps"] == 4
    assert summary["pairs_seen"] == 8
    assert summary["final_loss"] is not None
    final = Path(summary["checkpoints"][-1])
    assert final.name == "final.pt" and final.is_file()

    loaded = load_checkpoint(final)
    assert loaded["extra"]["step"] == 4
    assert loaded["extra"]["pairs_seen"] == 8
    assert loaded["optimizer_state"] is not None
    assert loaded["metadata"]["phase"] == "dpo"


def test_dpo_train_rejects_empty_pairs(tmp_path) -> None:
    model, tokenizer = _tiny()
    with pytest.raises(ValueError, match="DPO_EMPTY_PREFERENCE_PAIRS"):
        dpo_train(
            model, tokenizer, [], DpoConfig(max_steps=1),
            output_dir=tmp_path / "dpo",
        )


def test_dpo_train_accepts_repository_pair_shape(tmp_path) -> None:
    model, tokenizer = _tiny()
    # language_preference_pairs() 回傳的鍵形狀
    pairs = [
        {
            "pair_id": "star-pref-x",
            "intent": "reasoning",
            "prompt_text": "問題",
            "chosen_text": "好的答案內容",
            "rejected_text": "壞的",
            "source_type": "owner-governed-teaching-candidate",
        }
    ]
    summary = dpo_train(
        model, tokenizer, pairs, DpoConfig(max_steps=1, log_every=0),
        output_dir=tmp_path / "dpo",
    )
    assert summary["pairs"] == 1


class _FakePreferenceRepo:
    def __init__(self, rows):
        self._rows = rows

    def language_preference_pairs(self, *, limit=500):
        return self._rows[:limit]


def test_load_preference_pairs_filters_incomplete_rows() -> None:
    repo = _FakePreferenceRepo(
        [
            {
                "pair_id": "p1",
                "prompt_text": "  問題一  ",
                "chosen_text": "  好的答案  ",
                "rejected_text": "壞的",
            },
            {"pair_id": "p2", "prompt_text": "", "chosen_text": "x", "rejected_text": "y"},
            {"pair_id": "p3", "prompt_text": "q", "chosen_text": "", "rejected_text": "y"},
        ]
    )
    pairs = load_preference_pairs(repo)
    assert len(pairs) == 1
    assert pairs[0]["prompt_text"] == "問題一"  # 去空白
    assert pairs[0]["pair_id"] == "p1"


def test_dpo_train_from_repository_end_to_end(tmp_path) -> None:
    model, tokenizer = _tiny()
    repo = _FakePreferenceRepo(
        [
            {
                "pair_id": f"pair-{i}",
                "prompt_text": f"問題 {i}",
                "chosen_text": f"好的回答 {i}",
                "rejected_text": "壞",
            }
            for i in range(4)
        ]
    )
    summary = dpo_train_from_repository(
        model,
        tokenizer,
        repo,
        DpoConfig(max_steps=2, batch_size=2, log_every=0, checkpoint_every=0),
        output_dir=tmp_path / "dpo",
        limit=10,
    )
    assert summary["pairs"] == 4
    assert summary["source"] == "language_preference_pair"
    assert summary["pairs_available"] == 4
