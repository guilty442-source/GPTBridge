"""SFT training: template parity, prompt masking, smoke run, resume state."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

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
    SFTConfig,
    SFTDataset,
    encode_sft_example,
    read_sft_jsonl,
    sft_text,
    sft_train,
)


def _tiny() -> tuple[XingChengForCausalLM, XingChengTokenizer]:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    torch.manual_seed(7)
    return XingChengForCausalLM(cfg), XingChengTokenizer.from_config(cfg)


def _records() -> list[dict[str, str]]:
    return [
        {"prompt": "什麼是遞迴？", "completion": "遞迴是函式呼叫自身。"},
        {"prompt": "1+1 是多少？", "completion": "答案是 2。"},
        {"prompt": "列出水果", "completion": "蘋果、香蕉、橘子。"},
        {"prompt": "說明排序", "completion": "排序是將元素依大小排列。"},
    ]


def test_sft_template_matches_service_serializer() -> None:
    from xingcheng.infrastructure.sft_dataset import sft_text as service_sft_text

    assert sft_text("問題", "答案") == service_sft_text("問題", "答案")


def test_sft_encode_masks_prompt_and_keeps_completion() -> None:
    _, tokenizer = _tiny()
    pad_id = tokenizer.pad_id
    ids, labels = encode_sft_example(
        tokenizer, "問題", "答案", max_length=64, pad_id=pad_id
    )
    assert len(ids) == len(labels)
    prompt_ids = tokenizer.encode("問題\n\n", add_bos=True, add_eos=False)
    assert labels[: len(prompt_ids)] == [pad_id] * len(prompt_ids)
    assert any(token != pad_id for token in labels[len(prompt_ids) :])


def test_sft_dataset_accepts_snapshot_shapes() -> None:
    _, tokenizer = _tiny()
    dataset = SFTDataset(_records(), tokenizer, max_length=64)
    assert len(dataset) == 4
    ids, labels = dataset[0]
    assert len(ids) == len(labels) > 3


def test_sft_train_smoke_and_resume(tmp_path) -> None:
    model, tokenizer = _tiny()
    records = _records()
    summary = sft_train(
        model,
        tokenizer,
        records[:3],
        records[3:],
        SFTConfig(
            max_length=64,
            batch_size=2,
            grad_accum=1,
            lr=1e-2,
            max_steps=4,
            warmup_steps=1,
            eval_every=4,
            checkpoint_every=2,
            log_every=0,
            device="cpu",
        ),
        output_dir=tmp_path / "sft",
    )
    assert summary["steps"] == 4
    assert summary["train_examples"] == 3
    assert summary["final_loss"] is not None
    final = Path(summary["checkpoints"][-1])
    assert final.name == "final.pt" and final.is_file()

    loaded = load_checkpoint(final)
    assert loaded["extra"]["step"] == 4
    assert loaded["optimizer_state"] is not None
    assert loaded["metadata"]["phase"] == "supervised-fine-tuning"

    resumed = sft_train(
        loaded["model"],
        tokenizer,
        records[:3],
        records[3:],
        SFTConfig(
            max_length=64,
            batch_size=2,
            grad_accum=1,
            lr=1e-2,
            max_steps=6,
            warmup_steps=1,
            eval_every=3,
            checkpoint_every=0,
            log_every=0,
            device="cpu",
        ),
        output_dir=tmp_path / "sft-resume",
        resume=final,
    )
    assert resumed["start_step"] == 4
    assert resumed["steps"] == 6


def test_sft_train_rejects_empty_dataset(tmp_path) -> None:
    model, tokenizer = _tiny()
    with pytest.raises(ValueError, match="SFT_DATASET_EMPTY"):
        sft_train(
            model,
            tokenizer,
            [],
            [],
            SFTConfig(max_steps=1, device="cpu"),
            output_dir=tmp_path / "sft",
        )


def test_read_sft_jsonl_snapshot_roundtrip(tmp_path) -> None:
    snapshot = tmp_path / "sft.jsonl"
    snapshot.write_text(
        '{"prompt": "問題", "completion": "答案", "sha256": "x"}\n',
        encoding="utf-8",
    )
    records = read_sft_jsonl(snapshot)
    assert records[0]["prompt"] == "問題"
    assert records[0]["completion"] == "答案"
