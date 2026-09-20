"""Governed executor for transformer training jobs.

Drives queued jobs in :class:`TransformerTrainingRepository` through the
``queued -> preflight -> training -> validating -> completed`` state machine
using the native transformer training stack.  The executor only produces
checkpoint artefacts inside the tool root; it never mutates
``transformer_runtime_model_state`` and ``automatic_weight_replacement``
remains 0 — trained weights cannot replace runtime weights without the
separate governed release path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .transformer_training_repository import TransformerTrainingRepository


_ALLOWED_PRESETS = frozenset({"small", "medium", "base", "large"})

_ALLOWED_TRAINING_KINDS = frozenset({"pretrain", "sft", "dpo"})

_INT_BOUNDS = {
    "block_size": (512, 1, 4_096),
    "batch_size": (8, 1, 64),
    "grad_accum": (4, 1, 64),
    "max_steps": (1_000, 1, 10_000),
    "warmup_steps": (100, 0, 10_000),
    "checkpoint_every": (200, 0, 10_000),
    "eval_every": (100, 0, 10_000),
    "eval_batches": (8, 1, 512),
    "log_every": (10, 0, 10_000),
    "seed": (42, 0, 2**63 - 1),
    "max_train_documents": (0, 0, 10_000_000),
    "max_length": (512, 64, 4_096),
}


class TrainingJobExecutorError(RuntimeError):
    """Structured executor failure carrying a stable error code."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_snapshot_documents(path: Path) -> list[dict[str, str]]:
    documents: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TrainingJobExecutorError(
                    "EXECUTOR_SNAPSHOT_UNREADABLE",
                    f"snapshot line is not valid JSON: {exc}",
                ) from exc
            document = {
                "source": str(record.get("source") or ""),
                "text": str(record["text"]),
                "sha256": str(record["sha256"]),
            }
            if record.get("prompt"):
                document["prompt"] = str(record["prompt"])
            if record.get("completion"):
                document["completion"] = str(record["completion"])
            if record.get("chosen"):
                document["chosen"] = str(record["chosen"])
            if record.get("rejected"):
                document["rejected"] = str(record["rejected"])
            if isinstance(record.get("messages"), list):
                document["messages"] = record["messages"]
            documents.append(document)
    return documents


def _default_sft_train_fn(
    train_documents: list,
    val_documents: list,
    configuration: Mapping[str, Any],
    *,
    output_dir: Path,
    resume: Path | None,
) -> dict[str, Any]:
    """SFT job trainer: tokenizer + optional init checkpoint + masked loss."""
    from .native_transformer.bpe import NativeBPETokenizer
    from .native_transformer.checkpoint import load_checkpoint
    from .native_transformer.modules.model import XingChengForCausalLM
    from .native_transformer.training.pretrain import build_model_config
    from .native_transformer.training.sft import SFTConfig, sft_train

    tokenizer = NativeBPETokenizer.load(configuration["tokenizer_dir"])
    init_checkpoint = configuration.get("init_checkpoint") or resume
    if init_checkpoint:
        model = load_checkpoint(init_checkpoint, map_location="cpu")["model"]
    else:
        model = XingChengForCausalLM(
            build_model_config(
                str(configuration.get("preset") or "small"),
                tokenizer,
                int(configuration.get("max_length") or 512),
            )
        )
    config = SFTConfig(
        **{
            key: configuration[key]
            for key in SFTConfig.__dataclass_fields__
            if key in configuration
        }
    )
    summary = sft_train(
        model,
        tokenizer,
        train_documents,
        val_documents,
        config,
        output_dir=output_dir,
        resume=resume,
    )
    summary["final_checkpoint"] = str(Path(output_dir) / "final.pt")
    return summary


def _default_dpo_train_fn(
    train_documents: list,
    val_documents: list,
    configuration: Mapping[str, Any],
    *,
    output_dir: Path,
    resume: Path | None,
) -> dict[str, Any]:
    """DPO job trainer: init checkpoint 為 policy，凍結副本為 reference。"""
    from .native_transformer.bpe import NativeBPETokenizer
    from .native_transformer.checkpoint import load_checkpoint
    from .native_transformer.training.dpo import DpoConfig, dpo_train

    tokenizer = NativeBPETokenizer.load(configuration["tokenizer_dir"])
    init_checkpoint = configuration.get("init_checkpoint") or resume
    if init_checkpoint is None:
        raise TrainingJobExecutorError(
            "EXECUTOR_DPO_REQUIRES_INIT",
            "DPO requires init_checkpoint as policy/reference anchor",
        )
    model = load_checkpoint(init_checkpoint, map_location="cpu")["model"]
    pairs = [
        {
            "prompt_text": doc.get("prompt") or "",
            "chosen_text": doc["chosen"],
            "rejected_text": doc["rejected"],
        }
        for doc in (*train_documents, *val_documents)
        if doc.get("chosen") and doc.get("rejected")
    ]
    config = DpoConfig(
        beta=float(configuration.get("beta") or 0.1),
        lr=float(configuration["lr"]),
        max_steps=int(configuration["max_steps"]),
        batch_size=int(configuration["batch_size"]),
        max_seq_len=int(configuration.get("max_length") or 256),
        log_every=int(configuration.get("log_every") or 10),
        checkpoint_every=int(configuration.get("checkpoint_every") or 50),
        seed=int(configuration.get("seed") or 42),
        device=configuration.get("device"),
    )
    summary = dpo_train(
        model, tokenizer, pairs, config, output_dir=output_dir, resume=resume
    )
    summary["final_checkpoint"] = str(Path(output_dir) / "final.pt")
    return summary


def _default_train_fn(
    train_documents: list,
    val_documents: list,
    configuration: Mapping[str, Any],
    *,
    output_dir: Path,
    resume: Path | None,
) -> dict[str, Any]:
    """Default trainer: pretraining, masked-loss SFT, or DPO."""
    kind = str(configuration.get("training_kind") or "pretrain")
    if kind == "dpo":
        return _default_dpo_train_fn(
            train_documents,
            val_documents,
            configuration,
            output_dir=output_dir,
            resume=resume,
        )
    if kind == "sft":
        return _default_sft_train_fn(
            train_documents,
            val_documents,
            configuration,
            output_dir=output_dir,
            resume=resume,
        )
    import numpy as np
    import torch

    from .native_transformer.bpe import NativeBPETokenizer
    from .native_transformer.modules.model import XingChengForCausalLM
    from .native_transformer.training.pretrain import (
        PretrainConfig,
        build_model_config,
        encode_documents,
        pack_blocks,
        pretrain,
    )

    tokenizer = NativeBPETokenizer.load(configuration["tokenizer_dir"])
    train_ids = encode_documents(
        train_documents,
        tokenizer,
        limit=int(configuration.get("max_train_documents") or 0),
    )
    val_ids = encode_documents(val_documents, tokenizer)
    block_size = int(configuration["block_size"])
    train_blocks = torch.from_numpy(
        pack_blocks(train_ids, block_size).astype(np.int64)
    )
    val_blocks = torch.from_numpy(
        pack_blocks(val_ids, block_size).astype(np.int64)
    )
    model = XingChengForCausalLM(
        build_model_config(
            str(configuration.get("preset") or "small"), tokenizer, block_size
        )
    )
    config = PretrainConfig(
        **{
            key: configuration[key]
            for key in PretrainConfig.__dataclass_fields__
            if key in configuration
        }
    )
    return pretrain(
        model,
        tokenizer,
        train_blocks,
        val_blocks,
        config,
        output_dir=output_dir,
        resume=resume,
    )


class TrainingJobExecutor:
    """Executes governed training jobs and records auditable artefacts."""

    def __init__(
        self,
        repository: TransformerTrainingRepository,
        *,
        train_fn: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.repository = repository
        self.tool_root = Path(repository.tool_root).resolve()
        self._train_fn = train_fn or _default_train_fn

    # ------------------------------------------------------------- queries

    def _job_row(self, job_id: str) -> dict[str, Any]:
        with self.repository._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transformer_training_job WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
        if row is None:
            raise KeyError("transformer training job does not exist")
        return dict(row)

    def queued_jobs(self, *, limit: int = 16) -> list[dict[str, Any]]:
        with self.repository._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM transformer_training_job
                WHERE status = 'queued'
                ORDER BY created_at ASC LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [dict(row) for row in rows]

    def _dataset_and_splits(
        self, dataset_id: str
    ) -> tuple[dict[str, Any], dict[str, str]]:
        with self.repository._connect() as connection:
            dataset = connection.execute(
                "SELECT * FROM transformer_training_dataset WHERE dataset_id = ?",
                (str(dataset_id),),
            ).fetchone()
            examples = connection.execute(
                """
                SELECT content_sha256, split
                FROM transformer_training_dataset_example
                WHERE dataset_id = ?
                """,
                (str(dataset_id),),
            ).fetchall()
        if dataset is None:
            raise TrainingJobExecutorError(
                "EXECUTOR_DATASET_MISSING", "training dataset does not exist"
            )
        if str(dataset["state"]) != "prepared":
            raise TrainingJobExecutorError(
                "EXECUTOR_DATASET_NOT_PREPARED",
                "training dataset is not in prepared state",
            )
        return dict(dataset), {
            str(row["content_sha256"]): str(row["split"]) for row in examples
        }

    # ------------------------------------------------------------ preflight

    def _resolve_under_root(self, value: str, code: str) -> Path:
        candidate = Path(str(value or "").strip())
        if not candidate.is_absolute():
            candidate = self.tool_root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.tool_root):
            raise TrainingJobExecutorError(code, f"path escapes tool root: {value}")
        return candidate

    def _normalize_configuration(self, row: Mapping[str, Any]) -> dict[str, Any]:
        try:
            raw = json.loads(str(row["configuration_json"]))
        except json.JSONDecodeError as exc:
            raise TrainingJobExecutorError(
                "EXECUTOR_CONFIG_INVALID", "configuration_json is not valid JSON"
            ) from exc
        if not isinstance(raw, dict):
            raise TrainingJobExecutorError(
                "EXECUTOR_CONFIG_INVALID", "configuration_json must be an object"
            )

        configuration: dict[str, Any] = {}
        for key, (default, minimum, maximum) in _INT_BOUNDS.items():
            try:
                value = int(raw.get(key, default))
            except (TypeError, ValueError) as exc:
                raise TrainingJobExecutorError(
                    "EXECUTOR_CONFIG_INVALID", f"{key} must be an integer"
                ) from exc
            if not minimum <= value <= maximum:
                raise TrainingJobExecutorError(
                    "EXECUTOR_CONFIG_INVALID",
                    f"{key}={value} outside bounded range {minimum}..{maximum}",
                )
            configuration[key] = value

        try:
            lr = float(raw.get("lr", 3e-4))
        except (TypeError, ValueError) as exc:
            raise TrainingJobExecutorError(
                "EXECUTOR_CONFIG_INVALID", "lr must be numeric"
            ) from exc
        if not 0.0 < lr <= 0.1:
            raise TrainingJobExecutorError(
                "EXECUTOR_CONFIG_INVALID", "lr outside bounded range (0, 0.1]"
            )
        configuration["lr"] = lr

        try:
            beta = float(raw.get("beta", 0.1))
        except (TypeError, ValueError) as exc:
            raise TrainingJobExecutorError(
                "EXECUTOR_CONFIG_INVALID", "beta must be numeric"
            ) from exc
        if not 0.001 <= beta <= 1.0:
            raise TrainingJobExecutorError(
                "EXECUTOR_CONFIG_INVALID", "beta outside bounded range [0.001, 1.0]"
            )
        configuration["beta"] = beta

        preset = str(raw.get("preset") or "small")
        if preset not in _ALLOWED_PRESETS:
            raise TrainingJobExecutorError(
                "EXECUTOR_CONFIG_INVALID", f"unknown preset: {preset}"
            )
        configuration["preset"] = preset

        training_kind = str(raw.get("training_kind") or "pretrain").strip().casefold()
        if training_kind not in _ALLOWED_TRAINING_KINDS:
            raise TrainingJobExecutorError(
                "EXECUTOR_CONFIG_INVALID", f"unknown training_kind: {training_kind}"
            )
        configuration["training_kind"] = training_kind

        init_checkpoint = raw.get("init_checkpoint")
        if init_checkpoint:
            init_path = self._resolve_under_root(
                str(init_checkpoint), "EXECUTOR_INIT_CHECKPOINT_SCOPE_DENIED"
            )
            if not init_path.is_file():
                raise TrainingJobExecutorError(
                    "EXECUTOR_INIT_CHECKPOINT_MISSING",
                    f"initial checkpoint missing: {init_path}",
                )
            configuration["init_checkpoint"] = init_path
        else:
            configuration["init_checkpoint"] = None
        if training_kind == "dpo" and configuration["init_checkpoint"] is None:
            raise TrainingJobExecutorError(
                "EXECUTOR_DPO_REQUIRES_INIT",
                "DPO requires init_checkpoint as policy/reference anchor",
            )

        device = raw.get("device")
        configuration["device"] = str(device) if device else None

        model_id = str(raw.get("model_id") or "xingcheng-native").strip()
        configuration["model_id"] = model_id or "xingcheng-native"

        tokenizer_dir = str(raw.get("tokenizer_dir") or "").strip()
        if not tokenizer_dir:
            raise TrainingJobExecutorError(
                "EXECUTOR_CONFIG_INVALID", "tokenizer_dir is required"
            )
        tokenizer_path = self._resolve_under_root(
            tokenizer_dir, "EXECUTOR_TOKENIZER_SCOPE_DENIED"
        )
        if not (tokenizer_path / "tokenizer.json").is_file():
            raise TrainingJobExecutorError(
                "EXECUTOR_TOKENIZER_UNAVAILABLE",
                f"tokenizer artefact missing: {tokenizer_path}",
            )
        configuration["tokenizer_dir"] = tokenizer_path

        resume = raw.get("resume_checkpoint")
        if resume:
            resume_path = self._resolve_under_root(
                str(resume), "EXECUTOR_RESUME_SCOPE_DENIED"
            )
            if not resume_path.is_file():
                raise TrainingJobExecutorError(
                    "EXECUTOR_RESUME_MISSING",
                    f"resume checkpoint missing: {resume_path}",
                )
            configuration["resume_checkpoint"] = resume_path
        else:
            configuration["resume_checkpoint"] = None
        return configuration

    def _load_split_documents(
        self, dataset: Mapping[str, Any], splits: Mapping[str, str]
    ) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
        snapshot_path = Path(str(dataset["snapshot_path"]))
        if not snapshot_path.is_file():
            raise TrainingJobExecutorError(
                "EXECUTOR_SNAPSHOT_MISSING", "dataset snapshot file is missing"
            )
        if _sha256_file(snapshot_path) != str(dataset["snapshot_sha256"]):
            raise TrainingJobExecutorError(
                "EXECUTOR_SNAPSHOT_DRIFT",
                "snapshot SHA-256 drifted from registered digest",
            )
        documents = _read_snapshot_documents(snapshot_path)
        snapshot_hashes = {doc["sha256"] for doc in documents}
        registered = set(splits)
        if snapshot_hashes != registered:
            raise TrainingJobExecutorError(
                "EXECUTOR_SNAPSHOT_MISMATCH",
                "snapshot records do not match registered example hashes",
            )
        train_docs = [
            doc for doc in documents if splits[doc["sha256"]] == "train"
        ]
        val_docs = [
            doc for doc in documents if splits[doc["sha256"]] == "validation"
        ]
        if not train_docs or not val_docs:
            raise TrainingJobExecutorError(
                "EXECUTOR_EMPTY_SPLIT",
                "registered splits produced no train/validation documents",
            )
        return train_docs, val_docs

    # ------------------------------------------------------------ execution

    def _lifecycle(self, model_id: str):
        """載入（或建立）模型線的生命週期紀錄。"""
        from .native_transformer.lifecycle import ModelLifecycle

        directory = (
            self.tool_root / "runtime" / "models" / "lifecycle" / model_id
        ).resolve()
        if not directory.is_relative_to(self.tool_root):
            raise TrainingJobExecutorError(
                "EXECUTOR_LIFECYCLE_SCOPE_DENIED", "lifecycle path escapes tool root"
            )
        return ModelLifecycle.load_or_create(directory, model_id)

    @staticmethod
    def _lifecycle_dir(tool_root: Path, model_id: str) -> Path:
        return tool_root / "runtime" / "models" / "lifecycle" / model_id

    def _lifecycle_advance(self, lifecycle, target: str, reason: str) -> None:
        """把生命週期推進到目標態；FAILED／UNINITIALIZED 先恢復。

        轉移被拒（例如 READY→PRETRAINING 不可直達）時僅跳過記帳——
        生命週期是稽核簿記，不得因此中斷受管訓練任務。
        """
        if lifecycle.state == target:
            return
        try:
            if lifecycle.state == "FAILED":
                lifecycle.transition("INITIALIZED", reason="recovery after failure")
            if lifecycle.state == "UNINITIALIZED":
                lifecycle.transition("INITIALIZED", reason="bootstrap")
            lifecycle.transition(target, reason=reason)
        except ValueError:
            return
        lifecycle.save(self._lifecycle_dir(self.tool_root, lifecycle.model_id))

    def _lifecycle_fail(self, configuration, reason: str) -> None:
        try:
            lifecycle = self._lifecycle(str(configuration.get("model_id") or "xingcheng-native"))
            lifecycle.fail(reason[:200])
            lifecycle.save(self._lifecycle_dir(self.tool_root, lifecycle.model_id))
        except Exception:
            pass

    def _fail_job(self, job_id: str, code: str, message: str) -> dict[str, Any]:
        try:
            row = self.repository.transition_training_job(
                job_id,
                "failed",
                error_code=code,
                error_message=message,
            )
            return dict(row)
        except ValueError:
            return self._job_row(job_id)

    def _runtime_state(self) -> dict[str, Any]:
        with self.repository._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transformer_runtime_model_state WHERE singleton_id = 1"
            ).fetchone()
        return dict(row) if row is not None else {}

    def _record_execution_audit(
        self, job: Mapping[str, Any], summary: Mapping[str, Any]
    ) -> None:
        with self.repository._connect() as connection:
            self.repository._append_audit(
                connection,
                event_type="training-job-executed",
                entity_type="training-job",
                entity_id=str(job["job_id"]),
                payload={
                    "dataset_id": str(job["dataset_id"]),
                    "configuration_sha256": str(job["configuration_sha256"]),
                    "steps": summary.get("steps"),
                    "tokens_seen": summary.get("tokens_seen"),
                    "final_loss": summary.get("final_loss"),
                    "eval": summary.get("eval") or {},
                    "output_path": str(job.get("output_path") or ""),
                    "automatic_weight_replacement": int(
                        self._runtime_state().get(
                            "automatic_weight_replacement", 0
                        )
                    ),
                },
            )

    def run_job(self, job_id: str) -> dict[str, Any]:
        """Run one job through the governed state machine. Returns the final
        job row plus an executor report; failures transition the job to
        ``failed`` instead of raising."""
        job_id = str(job_id)
        row = self._job_row(job_id)
        if str(row["status"]) != "queued":
            raise TrainingJobExecutorError(
                "EXECUTOR_JOB_NOT_QUEUED",
                f"job is {row['status']}, not queued",
            )
        self.repository.transition_training_job(job_id, "preflight")
        lifecycle = None
        try:
            configuration = self._normalize_configuration(row)
            dataset, splits = self._dataset_and_splits(str(row["dataset_id"]))
            train_docs, val_docs = self._load_split_documents(dataset, splits)

            lifecycle = self._lifecycle(str(configuration["model_id"]))
            self._lifecycle_advance(
                lifecycle,
                "PRETRAINING"
                if configuration["training_kind"] == "pretrain"
                else "SFT_TRAINING",
                f"job {job_id} started",
            )
            self.repository.transition_training_job(job_id, "training")
            output_dir = (
                self.tool_root / "runtime" / "models" / "jobs" / job_id
            ).resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            try:
                summary = self._train_fn(
                    train_docs,
                    val_docs,
                    configuration,
                    output_dir=output_dir,
                    resume=configuration["resume_checkpoint"],
                )
            except TrainingJobExecutorError:
                raise
            except Exception as exc:
                raise TrainingJobExecutorError(
                    "EXECUTOR_TRAINING_FAILED", str(exc)[:500]
                ) from exc

            self.repository.transition_training_job(job_id, "validating")
            final_checkpoint = Path(
                str((summary or {}).get("final_checkpoint") or "")
                or output_dir / "final.pt"
            ).resolve()
            if not final_checkpoint.is_relative_to(self.tool_root):
                raise TrainingJobExecutorError(
                    "EXECUTOR_VALIDATION_FAILED",
                    "final checkpoint escapes tool root",
                )
            try:
                from .native_transformer.checkpoint import load_checkpoint

                load_checkpoint(final_checkpoint, map_location="cpu")
            except Exception as exc:
                raise TrainingJobExecutorError(
                    "EXECUTOR_VALIDATION_FAILED",
                    f"final checkpoint failed integrity verification: {exc}",
                ) from exc

            completed = self.repository.transition_training_job(
                job_id, "completed", output_path=str(final_checkpoint)
            )
            self._record_execution_audit(completed, summary or {})
            if lifecycle is not None:
                try:
                    lifecycle.register_artifact(
                        "weights",
                        final_checkpoint,
                        metadata={
                            "job_id": job_id,
                            "dataset_id": str(row["dataset_id"]),
                            "steps": (summary or {}).get("steps"),
                        },
                        activate=True,
                    )
                    self._lifecycle_advance(
                        lifecycle,
                        "PRETRAINED"
                        if configuration["training_kind"] == "pretrain"
                        else "INSTRUCT_READY",
                        f"job {job_id} completed",
                    )
                except Exception:
                    pass  # 生命週期紀錄失敗不影響已完成的訓練結果
            return {
                "ok": True,
                "job": completed,
                "output_path": str(final_checkpoint),
                "summary": summary or {},
                "automatic_weight_replacement": int(
                    self._runtime_state().get("automatic_weight_replacement", 0)
                ),
            }
        except TrainingJobExecutorError as exc:
            if lifecycle is not None:
                try:
                    lifecycle.fail(exc.error_code)
                    lifecycle.save(
                        self._lifecycle_dir(self.tool_root, lifecycle.model_id)
                    )
                except Exception:
                    pass
            failed = self._fail_job(job_id, exc.error_code, str(exc))
            return {
                "ok": False,
                "job": failed,
                "error_code": exc.error_code,
                "error_message": str(exc),
            }
        except Exception as exc:
            failed = self._fail_job(job_id, "EXECUTOR_INTERNAL_ERROR", str(exc))
            return {
                "ok": False,
                "job": failed,
                "error_code": "EXECUTOR_INTERNAL_ERROR",
                "error_message": str(exc)[:500],
            }

    def run_next(self) -> dict[str, Any] | None:
        """Claim and run the oldest queued job; None when the queue is empty."""
        queued = self.queued_jobs(limit=1)
        if not queued:
            return None
        return self.run_job(str(queued[0]["job_id"]))


__all__ = ["TrainingJobExecutor", "TrainingJobExecutorError"]
