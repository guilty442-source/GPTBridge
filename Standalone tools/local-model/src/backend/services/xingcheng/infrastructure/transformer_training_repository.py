from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence


class TransformerTrainingRepository:
    """Isolated persistence for governed Transformer adapter training.

    Role databases remain the owners of approved language examples.  This
    database stores immutable snapshot references, training jobs, adapter
    candidates, evaluations, releases and a hash-chained audit trail.  It does
    not grant the running model authority to replace its own base weights.
    """

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

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve() / "xingcheng"
        self.database_path = (
            self.tool_root / "runtime" / "state" / self.DATABASE_NAME
        ).resolve()
        if not self.database_path.is_relative_to(self.tool_root):
            raise PermissionError("TRANSFORMER_TRAINING_DATABASE_SCOPE_DENIED")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._migrate()

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

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        event_id = f"star-transformer-audit-{uuid.uuid4().hex[:24]}"
        created_at = self._now()
        previous = connection.execute(
            """
            SELECT event_sha256 FROM transformer_training_audit_event
            ORDER BY sequence DESC LIMIT 1
            """
        ).fetchone()
        previous_sha256 = str(previous[0]) if previous else "0" * 64
        payload_json = self._canonical_json(dict(payload))
        event_body = self._canonical_json(
            {
                "event_id": event_id,
                "event_type": str(event_type),
                "entity_type": str(entity_type),
                "entity_id": str(entity_id),
                "payload": json.loads(payload_json),
                "previous_event_sha256": previous_sha256,
                "created_at": created_at,
            }
        )
        event_sha256 = self._sha256_text(event_body)
        connection.execute(
            """
            INSERT INTO transformer_training_audit_event(
                event_id, event_type, entity_type, entity_id, payload_json,
                previous_event_sha256, event_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                str(event_type),
                str(entity_type),
                str(entity_id),
                payload_json,
                previous_sha256,
                event_sha256,
                created_at,
            ),
        )
        return {
            "event_id": event_id,
            "event_sha256": event_sha256,
            "previous_event_sha256": previous_sha256,
            "created_at": created_at,
        }

    def create_dataset(
        self,
        *,
        content_sha256: str,
        snapshot_path: str,
        snapshot_sha256: str,
        examples: Sequence[Mapping[str, Any]],
        source_manifest: Mapping[str, Any],
        created_by: str = "star-main-native-model",
        format_version: str = "star-transformer-sft/v1",
    ) -> dict[str, Any]:
        content_digest = self._require_sha256(content_sha256, "content_sha256")
        snapshot_digest = self._require_sha256(snapshot_sha256, "snapshot_sha256")
        normalized_path = str(snapshot_path or "").strip()
        if not normalized_path:
            raise ValueError("snapshot_path is required")
        snapshot_file = Path(normalized_path)
        if not snapshot_file.is_absolute():
            snapshot_file = self.tool_root / snapshot_file
        snapshot_file = snapshot_file.resolve()
        if not snapshot_file.is_relative_to(self.tool_root):
            raise PermissionError("TRANSFORMER_TRAINING_SNAPSHOT_SCOPE_DENIED")
        if not snapshot_file.is_file():
            raise FileNotFoundError("transformer training snapshot does not exist")
        actual_snapshot_digest = self._sha256_file(snapshot_file)
        if actual_snapshot_digest != snapshot_digest:
            raise ValueError("transformer training snapshot SHA-256 mismatch")
        normalized_examples: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for ordinal, example in enumerate(examples, start=1):
            split = str(example.get("split") or "").strip().casefold()
            scope = str(example.get("database_scope") or "").strip().casefold()
            item_hash = self._require_sha256(
                str(example.get("content_sha256") or ""),
                "example.content_sha256",
            )
            if split not in {"train", "validation"}:
                raise ValueError("example.split must be train or validation")
            if scope not in {"main", "investment", "mathematical", "coding"}:
                raise ValueError("example.database_scope is not supported")
            if item_hash in seen_hashes:
                raise ValueError("duplicate example content hash in dataset")
            seen_hashes.add(item_hash)
            quality = float(example.get("quality_score") or 0)
            if not 0.8 <= quality <= 1.0:
                raise ValueError("example quality must be between 0.8 and 1.0")
            source_revision = int(example.get("source_revision") or 0)
            if source_revision < 1:
                raise ValueError("example source_revision must be positive")
            normalized_examples.append(
                {
                    "ordinal": ordinal,
                    "split": split,
                    "owner_model_id": str(example.get("owner_model_id") or "").strip(),
                    "database_scope": scope,
                    "source_example_id": str(
                        example.get("source_example_id") or ""
                    ).strip(),
                    "source_revision": source_revision,
                    "content_sha256": item_hash,
                    "source_type": str(example.get("source_type") or "").strip(),
                    "quality_score": quality,
                }
            )
        train_count = sum(item["split"] == "train" for item in normalized_examples)
        validation_count = sum(
            item["split"] == "validation" for item in normalized_examples
        )
        if not normalized_examples or train_count < 1 or validation_count < 1:
            raise ValueError("dataset requires train and validation examples")
        if any(
            not item["owner_model_id"]
            or not item["source_example_id"]
            or not item["source_type"]
            for item in normalized_examples
        ):
            raise ValueError("dataset example ownership and provenance are required")
        dataset_id = f"star-transformer-dataset-{content_digest[:24]}"
        created_at = self._now()
        manifest_json = self._canonical_json(dict(source_manifest))
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM transformer_training_dataset
                WHERE content_sha256 = ?
                """,
                (content_digest,),
            ).fetchone()
            if existing is not None:
                return {**dict(existing), "inserted": False}
            connection.execute(
                """
                INSERT INTO transformer_training_dataset(
                    dataset_id, content_sha256, format_version, base_model_id,
                    runtime_model_id, example_count, training_example_count,
                    validation_example_count, minimum_quality_score,
                    source_manifest_json, snapshot_path, snapshot_sha256,
                    state, created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
                """,
                (
                    dataset_id,
                    content_digest,
                    str(format_version),
                    self.BASE_MODEL_ID,
                    self.RUNTIME_MODEL_ID,
                    len(normalized_examples),
                    train_count,
                    validation_count,
                    min(item["quality_score"] for item in normalized_examples),
                    manifest_json,
                    str(snapshot_file),
                    snapshot_digest,
                    str(created_by or "star-main-native-model"),
                    created_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO transformer_training_dataset_example(
                    dataset_id, ordinal, split, owner_model_id, database_scope,
                    source_example_id, source_revision, content_sha256,
                    source_type, quality_score
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        dataset_id,
                        item["ordinal"],
                        item["split"],
                        item["owner_model_id"],
                        item["database_scope"],
                        item["source_example_id"],
                        item["source_revision"],
                        item["content_sha256"],
                        item["source_type"],
                        item["quality_score"],
                    )
                    for item in normalized_examples
                ],
            )
            self._append_audit(
                connection,
                event_type="dataset-created",
                entity_type="training-dataset",
                entity_id=dataset_id,
                payload={
                    "content_sha256": content_digest,
                    "snapshot_sha256": snapshot_digest,
                    "example_count": len(normalized_examples),
                    "training_example_count": train_count,
                    "validation_example_count": validation_count,
                    "created_by": str(created_by),
                },
            )
            row = connection.execute(
                "SELECT * FROM transformer_training_dataset WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("transformer training dataset was not created")
        return {**dict(row), "inserted": True}

    def create_training_job(
        self,
        *,
        dataset_id: str,
        configuration: Mapping[str, Any],
        requested_by: str = "star-main-native-model",
        retry_of_job_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_dataset_id = str(dataset_id or "").strip()
        configuration_json = self._canonical_json(dict(configuration))
        configuration_sha256 = self._sha256_text(configuration_json)
        job_id = f"star-transformer-job-{uuid.uuid4().hex[:24]}"
        created_at = self._now()
        with self._connect() as connection:
            dataset = connection.execute(
                """
                SELECT state FROM transformer_training_dataset
                WHERE dataset_id = ?
                """,
                (normalized_dataset_id,),
            ).fetchone()
            if dataset is None:
                raise KeyError("transformer training dataset does not exist")
            if str(dataset[0]) != "prepared":
                raise ValueError("transformer training dataset is not prepared")
            connection.execute(
                """
                INSERT INTO transformer_training_job(
                    job_id, dataset_id, base_model_id, training_method,
                    configuration_json, configuration_sha256, status,
                    requested_by, retry_of_job_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    job_id,
                    normalized_dataset_id,
                    self.BASE_MODEL_ID,
                    self.TRAINING_METHOD,
                    configuration_json,
                    configuration_sha256,
                    str(requested_by or "star-main-native-model"),
                    str(retry_of_job_id).strip() if retry_of_job_id else None,
                    created_at,
                ),
            )
            self._append_audit(
                connection,
                event_type="training-job-created",
                entity_type="training-job",
                entity_id=job_id,
                payload={
                    "dataset_id": normalized_dataset_id,
                    "configuration_sha256": configuration_sha256,
                    "requested_by": str(requested_by),
                },
            )
            row = connection.execute(
                "SELECT * FROM transformer_training_job WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("transformer training job was not created")
        return dict(row)

    def transition_training_job(
        self,
        job_id: str,
        status: str,
        *,
        output_path: str = "",
        error_code: str = "",
        error_message: str = "",
    ) -> dict[str, Any]:
        requested_status = str(status or "").strip().casefold()
        if requested_status not in self.JOB_STATES:
            raise ValueError("unsupported transformer training job status")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transformer_training_job WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None:
                raise KeyError("transformer training job does not exist")
            current = str(row["status"])
            if requested_status not in self.JOB_TRANSITIONS[current]:
                raise ValueError(
                    f"invalid transformer training transition: {current} -> {requested_status}"
                )
            now = self._now()
            started_at = now if requested_status == "training" else str(row["started_at"])
            completed_at = (
                now
                if requested_status in {"completed", "failed", "cancelled"}
                else str(row["completed_at"])
            )
            connection.execute(
                """
                UPDATE transformer_training_job
                SET status = ?, output_path = ?, error_code = ?,
                    error_message = ?, started_at = ?, completed_at = ?
                WHERE job_id = ?
                """,
                (
                    requested_status,
                    str(output_path or row["output_path"]),
                    str(error_code)[:96],
                    str(error_message)[:1000],
                    started_at,
                    completed_at,
                    str(job_id),
                ),
            )
            self._append_audit(
                connection,
                event_type="training-job-transitioned",
                entity_type="training-job",
                entity_id=str(job_id),
                payload={
                    "from": current,
                    "to": requested_status,
                    "error_code": str(error_code)[:96],
                },
            )
            updated = connection.execute(
                "SELECT * FROM transformer_training_job WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
        if updated is None:
            raise RuntimeError("transformer training job transition was not stored")
        return dict(updated)

    def verify_audit_chain(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_id, event_type, entity_type, entity_id,
                       payload_json, previous_event_sha256, event_sha256,
                       created_at
                FROM transformer_training_audit_event
                ORDER BY sequence ASC
                """
            ).fetchall()
        expected_previous = "0" * 64
        for index, row in enumerate(rows, start=1):
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                return {
                    "ok": False,
                    "event_count": len(rows),
                    "failed_sequence": index,
                    "reason": "invalid-payload-json",
                }
            event_body = self._canonical_json(
                {
                    "event_id": str(row["event_id"]),
                    "event_type": str(row["event_type"]),
                    "entity_type": str(row["entity_type"]),
                    "entity_id": str(row["entity_id"]),
                    "payload": payload,
                    "previous_event_sha256": str(row["previous_event_sha256"]),
                    "created_at": str(row["created_at"]),
                }
            )
            if (
                str(row["previous_event_sha256"]) != expected_previous
                or self._sha256_text(event_body) != str(row["event_sha256"])
            ):
                return {
                    "ok": False,
                    "event_count": len(rows),
                    "failed_sequence": index,
                    "reason": "audit-chain-mismatch",
                }
            expected_previous = str(row["event_sha256"])
        return {
            "ok": True,
            "event_count": len(rows),
            "head_sha256": expected_previous,
        }

    def database_status(self) -> dict[str, Any]:
        with self._connect() as connection:
            integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            table_names = [
                "transformer_training_dataset",
                "transformer_training_dataset_example",
                "transformer_training_job",
                "transformer_adapter_candidate",
                "transformer_adapter_evaluation",
                "transformer_adapter_release",
                "transformer_runtime_model_state",
                "transformer_training_audit_event",
            ]
            tables = {
                table: int(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                )
                for table in table_names
            }
            state = connection.execute(
                "SELECT * FROM transformer_runtime_model_state WHERE singleton_id = 1"
            ).fetchone()
        audit = self.verify_audit_chain()
        return {
            "ok": integrity.casefold() == "ok" and audit["ok"],
            "engine": "local-sqlite3-degraded",
            "role": "owner-private-state-cache-checkpoint-or-bounded-reconciled-degraded-transport-only",
            "canonical_central_engine": "postgresql",
            "canonical": False,
            "authority": "non-canonical-reconciliation-required",
            "reconciliation_required": True,
            "schema": "star-transformer-training-database/v1",
            "schema_version": user_version,
            "path": str(self.database_path),
            "size_bytes": self.database_path.stat().st_size,
            "sqlite_integrity": integrity,
            "tables": tables,
            "audit_chain": audit,
            "runtime_model_state": dict(state) if state is not None else {},
            "base_weights_immutable": True,
            "automatic_weight_replacement": False,
            "role_database_ownership_preserved": True,
        }

    def maintain(self) -> dict[str, Any]:
        before = self.database_status()
        with self._connect() as connection:
            connection.execute("PRAGMA optimize")
            last_maintenance = connection.execute(
                """
                SELECT created_at FROM transformer_training_audit_event
                WHERE event_type = 'database-maintained'
                ORDER BY sequence DESC LIMIT 1
                """
            ).fetchone()
            should_record = last_maintenance is None
            if last_maintenance is not None:
                try:
                    last_at = datetime.fromisoformat(str(last_maintenance[0]))
                    should_record = (
                        datetime.now(timezone.utc) - last_at
                    ).total_seconds() >= 86_400
                except ValueError:
                    should_record = True
            if should_record or before["ok"] is not True:
                self._append_audit(
                    connection,
                    event_type="database-maintained",
                    entity_type="training-database",
                    entity_id=self.DATABASE_NAME,
                    payload={
                        "schema_version": self.SCHEMA_VERSION,
                        "integrity_before": before["sqlite_integrity"],
                        "audit_chain_before": before["audit_chain"]["ok"],
                    },
                )
        return self.database_status()


__all__ = ["TransformerTrainingRepository"]
