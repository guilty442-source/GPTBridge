from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


class TransformerTrainingSchemaMixin:
    """Schema definition, migration, and cryptographic helpers for the
    transformer training repository."""

    SCHEMA_VERSION = 1
    DATABASE_NAME = "transformer-training.sqlite3"
    BASE_MODEL_ID = "google/gemma-4-e2b-it"
    RUNTIME_MODEL_ID = "gemma4:e2b-it-qat"
    TRAINING_METHOD = "qlora-nf4-peft"
    DATASET_STATES = frozenset({"prepared", "invalidated", "archived"})
    JOB_STATES = frozenset(
        {
            "queued",
            "preflight",
            "training",
            "validating",
            "completed",
            "failed",
            "cancelled",
        }
    )
    ADAPTER_STATES = frozenset(
        {"candidate", "validated", "staged", "active", "rejected", "retired"}
    )
    JOB_TRANSITIONS: Mapping[str, frozenset[str]] = {
        "queued": frozenset({"preflight", "cancelled", "failed"}),
        "preflight": frozenset({"training", "cancelled", "failed"}),
        "training": frozenset({"validating", "cancelled", "failed"}),
        "validating": frozenset({"completed", "failed"}),
        "completed": frozenset(),
        "failed": frozenset(),
        "cancelled": frozenset(),
    }

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 15000")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;

                CREATE TABLE IF NOT EXISTS transformer_schema_metadata (
                    metadata_key TEXT PRIMARY KEY,
                    metadata_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS transformer_training_dataset (
                    dataset_id TEXT PRIMARY KEY,
                    content_sha256 TEXT NOT NULL UNIQUE,
                    format_version TEXT NOT NULL,
                    base_model_id TEXT NOT NULL,
                    runtime_model_id TEXT NOT NULL,
                    example_count INTEGER NOT NULL CHECK(example_count > 0),
                    training_example_count INTEGER NOT NULL
                        CHECK(training_example_count > 0),
                    validation_example_count INTEGER NOT NULL
                        CHECK(validation_example_count > 0),
                    minimum_quality_score REAL NOT NULL
                        CHECK(minimum_quality_score >= 0.8
                              AND minimum_quality_score <= 1.0),
                    source_manifest_json TEXT NOT NULL,
                    snapshot_path TEXT NOT NULL,
                    snapshot_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'prepared'
                        CHECK(state IN ('prepared', 'invalidated', 'archived')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    CHECK(example_count =
                          training_example_count + validation_example_count)
                );

                CREATE TABLE IF NOT EXISTS transformer_training_dataset_example (
                    dataset_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK(ordinal >= 1),
                    split TEXT NOT NULL CHECK(split IN ('train', 'validation')),
                    owner_model_id TEXT NOT NULL,
                    database_scope TEXT NOT NULL
                        CHECK(database_scope IN
                              ('main', 'investment', 'mathematical', 'coding')),
                    source_example_id TEXT NOT NULL,
                    source_revision INTEGER NOT NULL CHECK(source_revision >= 1),
                    content_sha256 TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    quality_score REAL NOT NULL
                        CHECK(quality_score >= 0.8 AND quality_score <= 1.0),
                    PRIMARY KEY(dataset_id, ordinal),
                    UNIQUE(dataset_id, content_sha256),
                    FOREIGN KEY(dataset_id)
                        REFERENCES transformer_training_dataset(dataset_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_transformer_dataset_example_source
                    ON transformer_training_dataset_example(
                        owner_model_id, source_revision
                    );

                CREATE TABLE IF NOT EXISTS transformer_training_job (
                    job_id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    base_model_id TEXT NOT NULL,
                    training_method TEXT NOT NULL,
                    configuration_json TEXT NOT NULL,
                    configuration_sha256 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN
                              ('queued', 'preflight', 'training', 'validating',
                               'completed', 'failed', 'cancelled')),
                    output_path TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    requested_by TEXT NOT NULL,
                    retry_of_job_id TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    completed_at TEXT NOT NULL DEFAULT '',
                    FOREIGN KEY(dataset_id)
                        REFERENCES transformer_training_dataset(dataset_id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(retry_of_job_id)
                        REFERENCES transformer_training_job(job_id)
                        ON DELETE RESTRICT
                );
                CREATE INDEX IF NOT EXISTS idx_transformer_training_job_status
                    ON transformer_training_job(status, created_at DESC);

                CREATE TABLE IF NOT EXISTS transformer_adapter_candidate (
                    adapter_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    dataset_id TEXT NOT NULL,
                    base_model_id TEXT NOT NULL,
                    adapter_format TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL UNIQUE,
                    metrics_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'candidate'
                        CHECK(status IN
                              ('candidate', 'validated', 'staged', 'active',
                               'rejected', 'retired')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(job_id)
                        REFERENCES transformer_training_job(job_id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(dataset_id)
                        REFERENCES transformer_training_dataset(dataset_id)
                        ON DELETE RESTRICT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_transformer_adapter
                    ON transformer_adapter_candidate(status)
                    WHERE status = 'active';

                CREATE TABLE IF NOT EXISTS transformer_adapter_evaluation (
                    evaluation_id TEXT PRIMARY KEY,
                    adapter_id TEXT NOT NULL,
                    suite_id TEXT NOT NULL,
                    suite_sha256 TEXT NOT NULL,
                    baseline_metrics_json TEXT NOT NULL,
                    adapter_metrics_json TEXT NOT NULL,
                    comparison_json TEXT NOT NULL,
                    quality_gates_json TEXT NOT NULL,
                    passed INTEGER NOT NULL CHECK(passed IN (0, 1)),
                    evaluated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(adapter_id, suite_sha256),
                    FOREIGN KEY(adapter_id)
                        REFERENCES transformer_adapter_candidate(adapter_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS transformer_adapter_release (
                    release_id TEXT PRIMARY KEY,
                    adapter_id TEXT NOT NULL,
                    action TEXT NOT NULL
                        CHECK(action IN
                              ('stage', 'activate', 'rollback', 'retire')),
                    previous_adapter_id TEXT,
                    governed_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(adapter_id)
                        REFERENCES transformer_adapter_candidate(adapter_id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(previous_adapter_id)
                        REFERENCES transformer_adapter_candidate(adapter_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS transformer_runtime_model_state (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
                    base_model_id TEXT NOT NULL,
                    runtime_model_id TEXT NOT NULL,
                    active_adapter_id TEXT,
                    previous_adapter_id TEXT,
                    automatic_weight_replacement INTEGER NOT NULL DEFAULT 0
                        CHECK(automatic_weight_replacement = 0),
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(active_adapter_id)
                        REFERENCES transformer_adapter_candidate(adapter_id)
                        ON DELETE RESTRICT,
                    FOREIGN KEY(previous_adapter_id)
                        REFERENCES transformer_adapter_candidate(adapter_id)
                        ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS transformer_training_audit_event (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_event_sha256 TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_transformer_training_audit_entity
                    ON transformer_training_audit_event(
                        entity_type, entity_id, sequence DESC
                    );

                CREATE TRIGGER IF NOT EXISTS protect_transformer_dataset_examples_update
                BEFORE UPDATE ON transformer_training_dataset_example
                BEGIN
                    SELECT RAISE(ABORT, 'TRANSFORMER_DATASET_SNAPSHOT_IMMUTABLE');
                END;
                CREATE TRIGGER IF NOT EXISTS protect_transformer_dataset_identity_update
                BEFORE UPDATE OF
                    dataset_id, content_sha256, format_version, base_model_id,
                    runtime_model_id, example_count, training_example_count,
                    validation_example_count, minimum_quality_score,
                    source_manifest_json, snapshot_path, snapshot_sha256,
                    created_by, created_at
                ON transformer_training_dataset
                BEGIN
                    SELECT RAISE(ABORT, 'TRANSFORMER_DATASET_SNAPSHOT_IMMUTABLE');
                END;
                CREATE TRIGGER IF NOT EXISTS protect_transformer_dataset_examples_delete
                BEFORE DELETE ON transformer_training_dataset_example
                BEGIN
                    SELECT RAISE(ABORT, 'TRANSFORMER_DATASET_SNAPSHOT_IMMUTABLE');
                END;
                CREATE TRIGGER IF NOT EXISTS protect_transformer_audit_update
                BEFORE UPDATE ON transformer_training_audit_event
                BEGIN
                    SELECT RAISE(ABORT, 'TRANSFORMER_TRAINING_AUDIT_IMMUTABLE');
                END;
                CREATE TRIGGER IF NOT EXISTS protect_transformer_audit_delete
                BEFORE DELETE ON transformer_training_audit_event
                BEGIN
                    SELECT RAISE(ABORT, 'TRANSFORMER_TRAINING_AUDIT_IMMUTABLE');
                END;
                """
            )
            now = self._now()
            connection.execute(
                """
                INSERT INTO transformer_schema_metadata(
                    metadata_key, metadata_value, updated_at
                ) VALUES ('schema_version', ?, ?)
                ON CONFLICT(metadata_key) DO UPDATE SET
                    metadata_value = excluded.metadata_value,
                    updated_at = excluded.updated_at
                """,
                (str(self.SCHEMA_VERSION), now),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO transformer_runtime_model_state(
                    singleton_id, base_model_id, runtime_model_id,
                    active_adapter_id, previous_adapter_id,
                    automatic_weight_replacement, updated_at
                ) VALUES (1, ?, ?, NULL, NULL, 0, ?)
                """,
                (self.BASE_MODEL_ID, self.RUNTIME_MODEL_ID, now),
            )
            connection.execute(
                """
                UPDATE transformer_runtime_model_state
                SET base_model_id = ?, runtime_model_id = ?, updated_at = ?
                WHERE singleton_id = 1 AND active_adapter_id IS NULL
                """,
                (self.BASE_MODEL_ID, self.RUNTIME_MODEL_ID, now),
            )
            connection.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _canonical_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _sha256_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @classmethod
    def _require_sha256(cls, value: str, field: str) -> str:
        normalized = str(value or "").strip().casefold()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError(f"{field} must be a hexadecimal SHA-256 digest")
        return normalized
