"""Governed training-job executor: state machine, scope, and failure paths."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import hashlib
import json
import sys
from pathlib import Path

import pytest

from xingcheng.infrastructure.native_transformer.checkpoint import save_checkpoint
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


def _tokenizer_dir(tmp_path: Path) -> Path:
    directory = _tool_root(tmp_path) / "runtime" / "models" / "tokenizer"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "tokenizer.json").write_text("{}", encoding="utf-8")
    return directory


def _snapshot(tmp_path: Path) -> tuple[Path, str]:
    path = (
        _tool_root(tmp_path)
        / "runtime"
        / "state"
        / "transformer-training"
        / "snapshot.jsonl"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {"source": "governed/main", "sha256": _sha("main-example"), "text": "第一方訓練語料內容"},
        {"source": "governed/coding", "sha256": _sha("coding-example"), "text": "def add(a, b): return a + b"},
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def _examples() -> list[dict[str, object]]:
    return [
        {
            "split": "train",
            "owner_model_id": "star-main-native-model",
            "database_scope": "main",
            "source_example_id": "star-train-main-1",
            "source_revision": 1,
            "content_sha256": _sha("main-example"),
            "source_type": "self-distillation-grounded",
            "quality_score": 0.91,
        },
        {
            "split": "validation",
            "owner_model_id": "star-coding-native-model",
            "database_scope": "coding",
            "source_example_id": "star-train-coding-1",
            "source_revision": 1,
            "content_sha256": _sha("coding-example"),
            "source_type": "chatgpt-governed-training-candidate",
            "quality_score": 0.94,
        },
    ]


def _dataset(repository, tmp_path: Path) -> dict:
    snapshot, snapshot_sha = _snapshot(tmp_path)
    return repository.create_dataset(
        content_sha256=_sha("executor-dataset"),
        snapshot_path=str(snapshot),
        snapshot_sha256=snapshot_sha,
        examples=_examples(),
        source_manifest={"roles": ["main", "coding"]},
    )


def _fake_train_fn(train_docs, val_docs, configuration, *, output_dir, resume):
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    model = XingChengForCausalLM(cfg)
    save_checkpoint(
        Path(output_dir) / "final.pt",
        model,
        metadata={"phase": "pretraining"},
        extra={"step": 4, "tokens_seen": 128},
    )
    return {
        "steps": 4,
        "tokens_seen": 128,
        "final_loss": 1.25,
        "eval": {"loss": 1.3, "perplexity": 3.67, "batches": 1},
        "checkpoints": [str(Path(output_dir) / "final.pt")],
    }


def _queued_job(repository, tmp_path: Path, **configuration) -> dict:
    dataset = _dataset(repository, tmp_path)
    _tokenizer_dir(tmp_path)
    config = {"tokenizer_dir": "runtime/models/tokenizer", **configuration}
    return repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]), configuration=config
    )


def test_executor_completes_job_through_governed_state_machine(
    tmp_path: Path,
) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(repository, tmp_path, max_steps=4)
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)

    result = executor.run_job(str(job["job_id"]))

    assert result["ok"] is True
    assert result["job"]["status"] == "completed"
    assert result["job"]["output_path"] == result["output_path"]
    assert Path(result["output_path"]).is_file()
    assert Path(result["output_path"]).is_relative_to(_tool_root(tmp_path))
    assert result["automatic_weight_replacement"] == 0
    assert result["job"]["started_at"]
    assert result["job"]["completed_at"]


def test_executor_records_execution_audit_and_preserves_weight_lock(
    tmp_path: Path,
) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(repository, tmp_path)
    TrainingJobExecutor(repository, train_fn=_fake_train_fn).run_job(
        str(job["job_id"])
    )

    status = repository.database_status()
    assert status["automatic_weight_replacement"] is False
    assert status["runtime_model_state"]["active_adapter_id"] is None
    assert status["audit_chain"]["ok"] is True
    with repository._connect() as connection:
        events = connection.execute(
            "SELECT event_type, payload_json FROM transformer_training_audit_event"
        ).fetchall()
    kinds = [str(row[0]) for row in events]
    assert "training-job-executed" in kinds
    payload = json.loads(
        str(next(r[1] for r in events if r[0] == "training-job-executed"))
    )
    assert payload["automatic_weight_replacement"] == 0
    assert payload["eval"]["perplexity"] == pytest.approx(3.67)


def test_executor_run_next_drains_oldest_queued_job(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    first = _queued_job(repository, tmp_path)
    second = _queued_job(repository, tmp_path)
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)

    result = executor.run_next()

    assert result is not None
    assert result["job"]["job_id"] == first["job_id"]
    assert executor.queued_jobs()[0]["job_id"] == second["job_id"]


def test_executor_run_next_returns_none_on_empty_queue(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)

    assert executor.run_next() is None


def test_executor_rejects_non_queued_job(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(repository, tmp_path)
    repository.transition_training_job(str(job["job_id"]), "preflight")
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)

    with pytest.raises(TrainingJobExecutorError, match="not queued"):
        executor.run_job(str(job["job_id"]))


def test_executor_fails_job_on_out_of_bounds_configuration(
    tmp_path: Path,
) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(repository, tmp_path, max_steps=9_999_999)
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)

    result = executor.run_job(str(job["job_id"]))

    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_CONFIG_INVALID"
    assert result["job"]["status"] == "failed"


def test_executor_fails_job_when_tokenizer_artefact_missing(
    tmp_path: Path,
) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    dataset = _dataset(repository, tmp_path)
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={"tokenizer_dir": "runtime/models/missing-tokenizer"},
    )
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)

    result = executor.run_job(str(job["job_id"]))

    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_TOKENIZER_UNAVAILABLE"
    assert result["job"]["status"] == "failed"


def test_executor_fails_closed_on_snapshot_digest_drift(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    dataset = _dataset(repository, tmp_path)
    _tokenizer_dir(tmp_path)
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={"tokenizer_dir": "runtime/models/tokenizer"},
    )
    Path(str(dataset["snapshot_path"])).write_text(
        json.dumps(
            {"source": "tampered", "sha256": _sha("x"), "text": "tampered"}
        )
        + "\n",
        encoding="utf-8",
    )
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)

    result = executor.run_job(str(job["job_id"]))

    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_SNAPSHOT_DRIFT"
    assert result["job"]["status"] == "failed"


def test_executor_fails_when_snapshot_content_not_registered(
    tmp_path: Path,
) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    snapshot, _ = _snapshot(tmp_path)
    records = [
        {"source": "rogue", "sha256": _sha("rogue-1"), "text": "unregistered"},
        {"source": "rogue", "sha256": _sha("rogue-2"), "text": "unregistered"},
    ]
    snapshot.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
        encoding="utf-8",
    )
    dataset = repository.create_dataset(
        content_sha256=_sha("executor-dataset"),
        snapshot_path=str(snapshot),
        snapshot_sha256=hashlib.sha256(snapshot.read_bytes()).hexdigest(),
        examples=_examples(),
        source_manifest={},
    )
    _tokenizer_dir(tmp_path)
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={"tokenizer_dir": "runtime/models/tokenizer"},
    )
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)

    result = executor.run_job(str(job["job_id"]))

    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_SNAPSHOT_MISMATCH"
    assert result["job"]["status"] == "failed"


def test_executor_fails_job_when_trainer_raises(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(repository, tmp_path)

    def _explode(train_docs, val_docs, configuration, *, output_dir, resume):
        raise RuntimeError("CUDA is on fire")

    result = TrainingJobExecutor(repository, train_fn=_explode).run_job(
        str(job["job_id"])
    )

    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_TRAINING_FAILED"
    assert result["job"]["status"] == "failed"
    assert "CUDA" in result["job"]["error_message"]


def test_executor_fails_when_final_checkpoint_is_tampered(
    tmp_path: Path,
) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(repository, tmp_path)

    def _corrupt(train_docs, val_docs, configuration, *, output_dir, resume):
        _fake_train_fn(
            train_docs, val_docs, configuration,
            output_dir=output_dir, resume=resume,
        )
        final = Path(output_dir) / "final.pt"
        final.write_bytes(b"corrupted-not-a-checkpoint")
        return {"final_checkpoint": str(final)}

    result = TrainingJobExecutor(repository, train_fn=_corrupt).run_job(
        str(job["job_id"])
    )

    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_VALIDATION_FAILED"
    assert result["job"]["status"] == "failed"


def test_executor_fails_closed_when_gpu_budget_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """G5：CUDA 訓練在 GpuCoordinator 取不到 VRAM 額度時 fail-closed 為
    EXECUTOR_GPU_BUSY，不進入訓練子程序、不與推論互相 OOM。"""
    from shared_layer.adaptive import gpu_coordinator

    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(
        repository,
        tmp_path,
        device="cuda",
        gpu_required_mb=2500,
        gpu_acquire_timeout_s=0,
    )

    def _deny(self, required_mb, priority="training", timeout=300):
        raise TimeoutError(
            f"GPU acquire timeout: need {required_mb}MB free (priority {priority})"
        )

    monkeypatch.setattr(gpu_coordinator.GpuCoordinator, "acquire", _deny)

    executor = TrainingJobExecutor(repository)  # 真實訓練器才參與 GPU 閘門

    def _must_not_run(*_args, **_kwargs):
        raise AssertionError("trainer must not start when VRAM is unavailable")

    monkeypatch.setattr(executor, "_invoke_trainer", _must_not_run)
    result = executor.run_job(str(job["job_id"]))

    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_GPU_BUSY"
    assert result["job"]["status"] == "failed"
    assert "GPU acquire timeout" in result["job"]["error_message"]


def _subprocess_executor(repository) -> TrainingJobExecutor:
    """Executor bound to the real trainer so ``_invoke_trainer`` takes the
    governed subprocess path (injected stubs stay in-process)."""
    return TrainingJobExecutor(repository)


def test_executor_kills_subprocess_on_rss_overbudget(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    executor = _subprocess_executor(repository)
    output_dir = tmp_path / "job-rss"
    with pytest.raises(TrainingJobExecutorError) as exc_info:
        executor._invoke_trainer(
            [{"source": "t", "text": "hello", "sha256": _sha("hello")}],
            [],
            {
                "train_max_rss_mb": 1,
                "resource_sample_interval_s": 1,
                "train_process_timeout_s": 120,
            },
            output_dir=output_dir,
            resume=None,
        )
    assert exc_info.value.error_code == "EXECUTOR_RESOURCE_OVERBUDGET"
    error = json.loads((output_dir / "train-error.json").read_text())
    assert "resource budget exceeded" in error["error"]


def test_executor_kills_subprocess_on_process_timeout(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    executor = _subprocess_executor(repository)
    with pytest.raises(TrainingJobExecutorError, match="timed out"):
        executor._invoke_trainer(
            [{"source": "t", "text": "hello", "sha256": _sha("hello")}],
            [],
            {"train_process_timeout_s": 1, "resource_sample_interval_s": 1},
            output_dir=tmp_path / "job-timeout",
            resume=None,
        )


def test_executor_fails_closed_when_rss_monitor_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)
    repository = TransformerTrainingRepository(tmp_path)
    executor = _subprocess_executor(repository)
    with pytest.raises(TrainingJobExecutorError) as exc_info:
        executor._invoke_trainer(
            [{"source": "t", "text": "hello", "sha256": _sha("hello")}],
            [],
            {"train_max_rss_mb": 128},
            output_dir=tmp_path / "job-nomon",
            resume=None,
        )
    assert exc_info.value.error_code == "EXECUTOR_RESOURCE_MONITOR_UNAVAILABLE"


def test_executor_fails_job_on_out_of_bounds_resource_budget(
    tmp_path: Path,
) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(repository, tmp_path, train_max_rss_mb=2_000_000)
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)
    result = executor.run_job(str(job["job_id"]))
    assert result["ok"] is False
    assert result["error_code"] == "EXECUTOR_CONFIG_INVALID"


def test_executor_normalizes_resource_budget_keys(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    job = _queued_job(
        repository,
        tmp_path,
        train_process_timeout_s=60,
        train_max_rss_mb=512,
        resource_sample_interval_s=2,
    )
    executor = TrainingJobExecutor(repository, train_fn=_fake_train_fn)
    config = executor._normalize_configuration(executor._job_row(str(job["job_id"])))
    assert config["train_process_timeout_s"] == 60
    assert config["train_max_rss_mb"] == 512
    assert config["resource_sample_interval_s"] == 2
