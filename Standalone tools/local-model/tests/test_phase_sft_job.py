"""Governed SFT training job: dataset snapshot, masked-loss run, checkpoint."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import hashlib
import json
from pathlib import Path

import torch

from xingcheng.infrastructure.native_transformer.bpe import NativeBPETokenizer
from xingcheng.infrastructure.native_transformer.checkpoint import (
    load_checkpoint,
    save_checkpoint,
)
from xingcheng.infrastructure.native_transformer.config import XingChengConfig
from xingcheng.infrastructure.native_transformer.modules.model import (
    XingChengForCausalLM,
)
from xingcheng.infrastructure.training_job_executor import (
    TrainingJobExecutor,
    TrainingJobExecutorError,
)
from xingcheng.infrastructure.transformer_training_repository import (
    TransformerTrainingRepository,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _tool_root(tmp_path: Path) -> Path:
    return tmp_path / "xingcheng"


def _train_tokenizer(tool_root: Path) -> Path:
    directory = tool_root / "runtime" / "models" / "tokenizer"
    texts = [
        "星澄是本地生成式語言模型。" * 20,
        "遞迴是函式呼叫自身。" * 20,
        "排序是將元素依大小排列。" * 20,
    ]
    NativeBPETokenizer.train(texts, directory, vocab_size=300, min_frequency=1)
    return directory


def _base_checkpoint(tool_root: Path, tokenizer_dir: Path) -> Path:
    tokenizer = NativeBPETokenizer.load(tokenizer_dir)
    config = XingChengConfig.small()
    config.vocab_size = tokenizer.vocab_size
    config.max_position_embeddings = 64
    torch.manual_seed(5)
    model = XingChengForCausalLM(config)
    path = tool_root / "runtime" / "models" / "base.pt"
    save_checkpoint(path, model, tokenizer=tokenizer, metadata={"phase": "base"})
    return path


def _snapshot(tool_root: Path) -> tuple[Path, str]:
    path = tool_root / "runtime" / "state" / "sft-snapshot.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    pairs = [
        ("什麼是遞迴？", "遞迴是函式呼叫自身。"),
        ("1+1 是多少？", "答案是 2。"),
        ("列出水果", "蘋果、香蕉、橘子。"),
        ("說明排序", "排序是將元素依大小排列。"),
    ]
    records = []
    for prompt, completion in pairs:
        text = f"{prompt}\n\n{completion}"
        records.append(
            {
                "source": "owner-governed-teaching-candidate",
                "sha256": _sha(text),
                "text": text,
                "prompt": prompt,
                "completion": completion,
                "split": "train",
            }
        )
    records[0]["split"] = "validation"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _examples(snapshot: Path) -> list[dict]:
    examples = []
    for index, line in enumerate(snapshot.read_text(encoding="utf-8").splitlines()):
        record = json.loads(line)
        examples.append(
            {
                "split": record["split"],
                "owner_model_id": "star-main-native-model",
                "database_scope": "main",
                "source_example_id": f"star-sft-{index}",
                "source_revision": 1,
                "content_sha256": record["sha256"],
                "source_type": record["source"],
                "quality_score": 0.91,
            }
        )
    return examples


def test_sft_job_runs_through_governed_state_machine(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    tool_root = _tool_root(tmp_path)
    tokenizer_dir = _train_tokenizer(tool_root)
    base = _base_checkpoint(tool_root, tokenizer_dir)
    snapshot, snapshot_sha = _snapshot(tool_root)
    dataset = repository.create_dataset(
        content_sha256=_sha("sft-dataset"),
        snapshot_path=str(snapshot),
        snapshot_sha256=snapshot_sha,
        examples=_examples(snapshot),
        source_manifest={"roles": ["main"]},
    )
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={
            "training_kind": "sft",
            "tokenizer_dir": "runtime/models/tokenizer",
            "init_checkpoint": "runtime/models/base.pt",
            "preset": "small",
            "max_length": 64,
            "batch_size": 2,
            "grad_accum": 1,
            "max_steps": 2,
            "warmup_steps": 0,
            "checkpoint_every": 0,
            "eval_every": 0,
            "log_every": 0,
            "device": "cpu",
        },
    )
    report = TrainingJobExecutor(repository).run_job(str(job["job_id"]))
    assert report["ok"] is True, report
    assert report["job"]["status"] == "completed"
    output = Path(report["output_path"])
    assert output.name == "final.pt" and output.is_file()
    loaded = load_checkpoint(output)
    assert loaded["metadata"]["phase"] == "supervised-fine-tuning"
    assert loaded["optimizer_state"] is not None
    assert report["automatic_weight_replacement"] == 0
    assert base.is_file()


def test_sft_job_requires_init_checkpoint_when_declared(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    tool_root = _tool_root(tmp_path)
    tokenizer_dir = _train_tokenizer(tool_root)
    assert tokenizer_dir.is_dir()
    snapshot, snapshot_sha = _snapshot(tool_root)
    dataset = repository.create_dataset(
        content_sha256=_sha("sft-dataset-2"),
        snapshot_path=str(snapshot),
        snapshot_sha256=snapshot_sha,
        examples=_examples(snapshot),
        source_manifest={"roles": ["main"]},
    )
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={
            "training_kind": "sft",
            "tokenizer_dir": "runtime/models/tokenizer",
            "init_checkpoint": "runtime/models/missing.pt",
            "max_steps": 1,
            "device": "cpu",
        },
    )
    report = TrainingJobExecutor(repository).run_job(str(job["job_id"]))
    assert report["ok"] is False
    assert report["error_code"] == "EXECUTOR_INIT_CHECKPOINT_MISSING"


def test_executor_rejects_unknown_training_kind(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    tool_root = _tool_root(tmp_path)
    _train_tokenizer(tool_root)
    snapshot, snapshot_sha = _snapshot(tool_root)
    dataset = repository.create_dataset(
        content_sha256=_sha("sft-dataset-3"),
        snapshot_path=str(snapshot),
        snapshot_sha256=snapshot_sha,
        examples=_examples(snapshot),
        source_manifest={"roles": ["main"]},
    )
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={
            "training_kind": "rlhf",
            "tokenizer_dir": "runtime/models/tokenizer",
            "max_steps": 1,
        },
    )
    report = TrainingJobExecutor(repository).run_job(str(job["job_id"]))
    assert report["ok"] is False
    assert report["error_code"] == "EXECUTOR_CONFIG_INVALID"
