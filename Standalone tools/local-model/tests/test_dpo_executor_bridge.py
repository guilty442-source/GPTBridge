# -*- coding: utf-8 -*-
"""DPO 受管鏈：偏好快照 → dataset → dpo job → executor 端到端。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import _xingcheng_test_support  # noqa: F401

from xingcheng.infrastructure.native_transformer.checkpoint import save_checkpoint
from xingcheng.infrastructure.native_transformer.config import XingChengConfig
from xingcheng.infrastructure.native_transformer.modules.model import (
    XingChengForCausalLM,
)
from xingcheng.infrastructure.preference_dataset_bridge import (
    build_pairs_snapshot,
    queue_dpo_job,
    register_pairs_snapshot,
)
from xingcheng.infrastructure.training_job_executor import (
    TrainingJobExecutor,
    TrainingJobExecutorError,
)
from xingcheng.infrastructure.transformer_training_repository import (
    TransformerTrainingRepository,
)

_ROOT_MARK = "xingcheng"


def _pairs(n: int) -> list[dict]:
    return [
        {
            "pair_id": f"pair-{i}",
            "prompt_text": f"問題 {i}：什麼是測試？",
            "chosen_text": f"正確回答 {i}。",
            "rejected_text": f"錯誤回答 {i}。",
            "intent": "chat",
        }
        for i in range(n)
    ]


def _manifest(tmp_path: Path, n: int = 6) -> dict:
    out = tmp_path / _ROOT_MARK / "runtime" / "state" / "dpo" / "pairs.jsonl"
    return build_pairs_snapshot(_pairs(n), out, val_permille=300)


def test_pairs_snapshot_deterministic_and_split(tmp_path: Path):
    manifest = _manifest(tmp_path)
    assert manifest["pairs"] == 6
    assert manifest["train_count"] >= 1 and manifest["validation_count"] >= 1
    records = [
        json.loads(line)
        for line in Path(manifest["snapshot_path"]).read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert all(r["chosen"] and r["rejected"] for r in records)
    # 冪等：同 pairs 重寫產生同雜湊
    again = build_pairs_snapshot(
        _pairs(6),
        tmp_path / _ROOT_MARK / "runtime" / "state" / "dpo" / "p2.jsonl",
        val_permille=300,
    )
    assert again["snapshot_sha256"] == manifest["snapshot_sha256"]


def test_register_and_queue_dpo_job(tmp_path: Path):
    repository = TransformerTrainingRepository(tmp_path)
    result = queue_dpo_job(
        repository,
        _manifest(tmp_path),
        configuration={"tokenizer_dir": "runtime/models/tokenizer"},
    )
    config = json.loads(result["job"]["configuration_json"])
    assert config["training_kind"] == "dpo"
    assert result["job"]["status"] == "queued"
    assert result["dataset"]["state"] == "prepared"


def test_executor_rejects_dpo_without_init_checkpoint(tmp_path: Path):
    repository = TransformerTrainingRepository(tmp_path)
    (tmp_path / _ROOT_MARK / "runtime" / "models" / "tokenizer").mkdir(
        parents=True, exist_ok=True
    )
    (tmp_path / _ROOT_MARK / "runtime" / "models" / "tokenizer" / "tokenizer.json").write_text(
        "{}", encoding="utf-8"
    )
    result = queue_dpo_job(
        repository,
        _manifest(tmp_path),
        configuration={"tokenizer_dir": "runtime/models/tokenizer"},
    )
    executor = TrainingJobExecutor(repository, train_fn=lambda *a, **k: {})
    outcome = executor.run_job(str(result["job"]["job_id"]))
    assert outcome["ok"] is False
    assert outcome["error_code"] == "EXECUTOR_DPO_REQUIRES_INIT"


def test_executor_dpo_end_to_end(tmp_path: Path):
    """真實 dpo_train：小模型＋真偏好對，走完整受管狀態機。"""
    import torch

    from xingcheng.infrastructure.native_transformer.bpe import train_bpe

    repository = TransformerTrainingRepository(tmp_path)
    tool_root = repository.tool_root
    tok_dir = tool_root / "runtime" / "models" / "tokenizer"
    train_bpe(
        ["星澄偏好訓練語料 問題 什麼是測試 正確回答 錯誤回答"] * 4,
        tok_dir,
        vocab_size=264,
        min_frequency=1,
    )

    # init checkpoint（policy/reference 起點）
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 128
    torch.manual_seed(3)
    init_dir = tool_root / "runtime" / "models" / "init"
    init_dir.mkdir(parents=True, exist_ok=True)
    save_checkpoint(init_dir / "init.pt", XingChengForCausalLM(cfg))

    result = queue_dpo_job(
        repository,
        _manifest(tmp_path),
        configuration={
            "tokenizer_dir": "runtime/models/tokenizer",
            "init_checkpoint": "runtime/models/init/init.pt",
            "training_kind": "dpo",
            "max_steps": 4,
            "batch_size": 2,
            "max_length": 64,
            "device": "cpu",
        },
    )
    executor = TrainingJobExecutor(repository)
    outcome = executor.run_job(str(result["job"]["job_id"]))
    assert outcome["ok"] is True, outcome.get("error_message")
    assert outcome["job"]["status"] == "completed"
    assert outcome["summary"]["pairs"] >= 5
    assert Path(outcome["output_path"]).is_file()

    # 生命週期：DPO 走 INSTRUCT_READY（post-training refinement）
    from xingcheng.infrastructure.native_transformer.lifecycle import (
        ModelLifecycle,
    )

    lc = ModelLifecycle.load(
        tool_root / "runtime" / "models" / "lifecycle" / "xingcheng-native"
    )
    assert lc.state == "INSTRUCT_READY"


def test_register_rejects_drift_and_scope(tmp_path: Path):
    repository = TransformerTrainingRepository(tmp_path)
    manifest = _manifest(tmp_path)
    drifted = dict(manifest, snapshot_sha256="0" * 64)
    with pytest.raises(ValueError, match="DRIFT"):
        register_pairs_snapshot(repository, drifted)
    outside = dict(manifest, snapshot_path=str(tmp_path.parent / "x.jsonl"))
    with pytest.raises(PermissionError, match="SCOPE"):
        register_pairs_snapshot(repository, outside)
