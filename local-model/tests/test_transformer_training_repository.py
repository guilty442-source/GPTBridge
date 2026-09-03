from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))

from xingcheng.application.service import LocalAiService
from xingcheng.infrastructure.transformer_runtime import StarTransformerRuntime
from xingcheng.infrastructure.transformer_training_repository import (
    TransformerTrainingRepository,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _snapshot(tmp_path: Path, content: str = "training snapshot\n") -> tuple[Path, str]:
    path = tmp_path / "runtime" / "state" / "transformer-training" / "snapshot.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
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


def _create_dataset(
    repository: TransformerTrainingRepository,
    tmp_path: Path,
) -> dict[str, object]:
    snapshot, snapshot_sha = _snapshot(tmp_path)
    return repository.create_dataset(
        content_sha256=_sha("semantic-dataset-content"),
        snapshot_path=str(snapshot),
        snapshot_sha256=snapshot_sha,
        examples=_examples(),
        source_manifest={"roles": ["main", "coding"], "raw_text_copied": False},
    )


def test_training_database_is_isolated_and_initialized(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    status = repository.database_status()

    assert status["ok"] is True
    assert status["schema_version"] == 1
    assert Path(status["path"]) == (
        tmp_path / "runtime" / "state" / "transformer-training.sqlite3"
    )
    assert status["tables"]["transformer_runtime_model_state"] == 1
    assert status["base_weights_immutable"] is True
    assert status["automatic_weight_replacement"] is False
    assert status["runtime_model_state"]["active_adapter_id"] is None


def test_training_database_migration_is_idempotent(tmp_path: Path) -> None:
    first = TransformerTrainingRepository(tmp_path).database_status()
    second = TransformerTrainingRepository(tmp_path).database_status()

    assert first["schema_version"] == second["schema_version"] == 1
    assert first["tables"] == second["tables"]


@pytest.mark.parametrize(
    "digest",
    ["", "abc", "g" * 64, "0" * 63, "0" * 65],
)
def test_dataset_rejects_invalid_sha256(tmp_path: Path, digest: str) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    snapshot, snapshot_sha = _snapshot(tmp_path)

    with pytest.raises(ValueError, match="SHA-256"):
        repository.create_dataset(
            content_sha256=digest,
            snapshot_path=str(snapshot),
            snapshot_sha256=snapshot_sha,
            examples=_examples(),
            source_manifest={},
        )


def test_dataset_snapshot_must_stay_in_tool_root(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path / "tool")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(PermissionError, match="SNAPSHOT_SCOPE_DENIED"):
        repository.create_dataset(
            content_sha256=_sha("dataset"),
            snapshot_path=str(outside),
            snapshot_sha256=hashlib.sha256(outside.read_bytes()).hexdigest(),
            examples=_examples(),
            source_manifest={},
        )


def test_dataset_snapshot_digest_is_verified(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    snapshot, _ = _snapshot(tmp_path)

    with pytest.raises(ValueError, match="snapshot SHA-256 mismatch"):
        repository.create_dataset(
            content_sha256=_sha("dataset"),
            snapshot_path=str(snapshot),
            snapshot_sha256="0" * 64,
            examples=_examples(),
            source_manifest={},
        )


def test_dataset_requires_both_train_and_validation_splits(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    snapshot, snapshot_sha = _snapshot(tmp_path)
    examples = _examples()
    examples[1]["split"] = "train"

    with pytest.raises(ValueError, match="train and validation"):
        repository.create_dataset(
            content_sha256=_sha("dataset"),
            snapshot_path=str(snapshot),
            snapshot_sha256=snapshot_sha,
            examples=examples,
            source_manifest={},
        )


def test_dataset_rejects_duplicate_example_content(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    snapshot, snapshot_sha = _snapshot(tmp_path)
    examples = _examples()
    examples[1]["content_sha256"] = examples[0]["content_sha256"]

    with pytest.raises(ValueError, match="duplicate example"):
        repository.create_dataset(
            content_sha256=_sha("dataset"),
            snapshot_path=str(snapshot),
            snapshot_sha256=snapshot_sha,
            examples=examples,
            source_manifest={},
        )


@pytest.mark.parametrize("quality", [0, 0.79, 1.01, 3])
def test_dataset_rejects_examples_outside_quality_gate(
    tmp_path: Path,
    quality: float,
) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    snapshot, snapshot_sha = _snapshot(tmp_path)
    examples = _examples()
    examples[0]["quality_score"] = quality

    with pytest.raises(ValueError, match="quality"):
        repository.create_dataset(
            content_sha256=_sha("dataset"),
            snapshot_path=str(snapshot),
            snapshot_sha256=snapshot_sha,
            examples=examples,
            source_manifest={},
        )


def test_dataset_records_only_provenance_links_and_hashes(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    created = _create_dataset(repository, tmp_path)
    status = repository.database_status()

    assert created["inserted"] is True
    assert created["example_count"] == 2
    assert created["training_example_count"] == 1
    assert created["validation_example_count"] == 1
    assert status["tables"]["transformer_training_dataset"] == 1
    assert status["tables"]["transformer_training_dataset_example"] == 2
    assert status["audit_chain"]["event_count"] == 1
    with repository._connect() as connection:
        columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(transformer_training_dataset_example)"
            ).fetchall()
        }
    assert "input_text" not in columns
    assert "target_text" not in columns


def test_dataset_registration_is_content_deduplicated(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    first = _create_dataset(repository, tmp_path)
    second = _create_dataset(repository, tmp_path)

    assert first["dataset_id"] == second["dataset_id"]
    assert second["inserted"] is False
    assert repository.database_status()["tables"]["transformer_training_dataset"] == 1


@pytest.mark.parametrize("operation", ["update", "delete"])
def test_dataset_snapshot_links_are_sqlite_immutable(
    tmp_path: Path,
    operation: str,
) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    created = _create_dataset(repository, tmp_path)

    with pytest.raises(sqlite3.IntegrityError, match="SNAPSHOT_IMMUTABLE"):
        with repository._connect() as connection:
            if operation == "update":
                connection.execute(
                    """
                    UPDATE transformer_training_dataset_example
                    SET source_type = 'changed' WHERE dataset_id = ?
                    """,
                    (created["dataset_id"],),
                )
            else:
                connection.execute(
                    """
                    DELETE FROM transformer_training_dataset_example
                    WHERE dataset_id = ?
                    """,
                    (created["dataset_id"],),
                )


def test_training_job_configuration_is_canonical_and_hashed(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    dataset = _create_dataset(repository, tmp_path)
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={"rank": 4, "max_sequence_length": 256, "learning_rate": 0.0002},
    )

    assert job["status"] == "queued"
    assert job["training_method"] == "qlora-nf4-peft"
    assert json.loads(job["configuration_json"])["rank"] == 4
    assert job["configuration_sha256"] == hashlib.sha256(
        str(job["configuration_json"]).encode("utf-8")
    ).hexdigest()


def test_training_job_enforces_forward_only_state_machine(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    dataset = _create_dataset(repository, tmp_path)
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={"rank": 4},
    )

    preflight = repository.transition_training_job(str(job["job_id"]), "preflight")
    training = repository.transition_training_job(str(job["job_id"]), "training")
    validating = repository.transition_training_job(str(job["job_id"]), "validating")
    completed = repository.transition_training_job(str(job["job_id"]), "completed")

    assert preflight["status"] == "preflight"
    assert training["started_at"]
    assert validating["status"] == "validating"
    assert completed["completed_at"]
    with pytest.raises(ValueError, match="invalid transformer training transition"):
        repository.transition_training_job(str(job["job_id"]), "training")


def test_training_job_cannot_skip_preflight(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    dataset = _create_dataset(repository, tmp_path)
    job = repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={"rank": 4},
    )

    with pytest.raises(ValueError, match="queued -> training"):
        repository.transition_training_job(str(job["job_id"]), "training")


def test_training_job_requires_registered_prepared_dataset(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)

    with pytest.raises(KeyError, match="dataset does not exist"):
        repository.create_training_job(
            dataset_id="star-transformer-dataset-missing",
            configuration={"rank": 4},
        )


def test_audit_events_are_hash_chained_and_immutable(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    dataset = _create_dataset(repository, tmp_path)
    repository.create_training_job(
        dataset_id=str(dataset["dataset_id"]),
        configuration={"rank": 4},
    )
    audit = repository.verify_audit_chain()

    assert audit["ok"] is True
    assert audit["event_count"] == 2
    assert audit["head_sha256"] != "0" * 64
    with pytest.raises(sqlite3.IntegrityError, match="AUDIT_IMMUTABLE"):
        with repository._connect() as connection:
            connection.execute(
                "UPDATE transformer_training_audit_event SET event_type = 'changed'"
            )


def test_maintenance_is_bounded_to_one_routine_audit_per_day(tmp_path: Path) -> None:
    repository = TransformerTrainingRepository(tmp_path)
    first = repository.maintain()
    second = repository.maintain()

    assert first["ok"] is second["ok"] is True
    assert first["audit_chain"]["event_count"] == 1
    assert second["audit_chain"]["event_count"] == 1


def test_service_status_exposes_training_database_without_weight_authority(
    tmp_path: Path,
) -> None:
    service = LocalAiService(
        tmp_path,
        transformer_runtime=StarTransformerRuntime(enabled=False),
    )
    _, status = asyncio.run(service.handle("xingcheng_status", {}))
    training_database = status["transformer_training_database"]

    assert training_database["ok"] is True
    assert training_database["schema"] == "star-transformer-training-database/v1"
    assert training_database["automatic_weight_replacement"] is False
    assert service.owns("xingcheng_transformer_training_status") is False
