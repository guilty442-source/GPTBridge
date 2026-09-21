# -*- coding: utf-8 -*-
"""工具呼叫訓練集產生器 + 快照合併 + executor messages 透傳。"""
from __future__ import annotations

import json
from pathlib import Path

import _xingcheng_test_support  # noqa: F401

from xingcheng.application.native_tool_orchestrator import TOOL_COMMAND_MAP
from xingcheng.application.tool_call_dataset import build_tool_call_snapshot
from xingcheng.infrastructure.distill_dataset_bridge import (
    merge_sft_snapshots,
    queue_distillation_sft_job,
    register_distillation_snapshot,
)
from xingcheng.infrastructure.training_job_executor import (
    _read_snapshot_documents,
)
from xingcheng.infrastructure.transformer_training_repository import (
    TransformerTrainingRepository,
)


def test_tool_call_snapshot_covers_all_tools(tmp_path: Path):
    manifest = build_tool_call_snapshot(tmp_path / "x" / "tool.jsonl")
    records = [
        json.loads(line)
        for line in Path(manifest["snapshot_path"])
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert manifest["examples"] == len(records) == len(TOOL_COMMAND_MAP) * 3 + len(
        TOOL_COMMAND_MAP
    )
    # 每筆都是 messages 多輪格式且含 tool_call 標記
    for record in records:
        assert record["messages"][0]["role"] == "system"
        assert any(
            "<tool_call>" in m["content"]
            for m in record["messages"]
            if m["role"] == "assistant"
        )
    # 工具名全覆蓋
    called = set()
    for record in records:
        for m in record["messages"]:
            for name in TOOL_COMMAND_MAP:
                if f'"name": "{name}"' in m["content"]:
                    called.add(name)
    assert called == set(TOOL_COMMAND_MAP)


def test_tool_call_snapshot_registers_and_queues(tmp_path: Path):
    repository = TransformerTrainingRepository(tmp_path)
    manifest = build_tool_call_snapshot(
        repository.tool_root / "runtime" / "state" / "tool-call" / "s.jsonl"
    )
    dataset = register_distillation_snapshot(repository, manifest)
    assert dataset["state"] == "prepared"
    assert dataset["example_count"] == manifest["examples"]


def test_executor_reads_messages_field(tmp_path: Path):
    repository = TransformerTrainingRepository(tmp_path)
    manifest = build_tool_call_snapshot(
        repository.tool_root / "runtime" / "state" / "tool-call" / "s.jsonl"
    )
    docs = _read_snapshot_documents(Path(manifest["snapshot_path"]))
    with_messages = [d for d in docs if "messages" in d]
    assert len(with_messages) == manifest["examples"]
    assert with_messages[0]["messages"][0]["role"] == "system"


def test_merge_snapshots_dedupes_and_preserves_messages(tmp_path: Path):
    a = tmp_path / "a.jsonl"
    b = tmp_path / "b.jsonl"
    rec = {
        "source": "s", "sha256": "a" * 64, "text": "t", "prompt": "p",
        "completion": "c", "quality_score": 0.9, "split": "train",
    }
    rec_msg = dict(rec, sha256="b" * 64,
                   messages=[{"role": "user", "content": "嗨"},
                             {"role": "assistant", "content": "你好"}])
    a.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")
    b.write_text(
        json.dumps(rec, ensure_ascii=False) + "\n"
        + json.dumps(rec_msg, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    manifest = merge_sft_snapshots(
        [a, b], tmp_path / "merged.jsonl", val_permille=100
    )
    assert manifest["examples"] == 2  # 重複 sha 去掉
    merged = [
        json.loads(l) for l in (tmp_path / "merged.jsonl")
        .read_text(encoding="utf-8").splitlines()
    ]
    assert any("messages" in r for r in merged)
    # 可註冊
    repository = TransformerTrainingRepository(tmp_path)
    in_root = repository.tool_root / "runtime" / "state" / "m" / "merged.jsonl"
    manifest2 = merge_sft_snapshots([a, b], in_root, val_permille=100)
    dataset = register_distillation_snapshot(repository, manifest2)
    assert dataset["example_count"] == 2
