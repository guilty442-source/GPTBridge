from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence

from .training_repo_schema import TransformerTrainingSchemaMixin


_DATASET_INSERT_SQL = """
            INSERT INTO transformer_training_dataset(
                dataset_id, content_sha256, format_version, base_model_id,
                runtime_model_id, example_count, training_example_count,
                validation_example_count, minimum_quality_score,
                source_manifest_json, snapshot_path, snapshot_sha256,
                state, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
            """

_EXAMPLE_INSERT_SQL = """
            INSERT INTO transformer_training_dataset_example(
                dataset_id, ordinal, split, owner_model_id, database_scope,
                source_example_id, source_revision, content_sha256,
                source_type, quality_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """

_DATASET_COLUMNS = (
    "dataset_id, content_sha256, format_version, base_model_id, "
    "runtime_model_id, example_count, training_example_count, "
    "validation_example_count, minimum_quality_score, source_manifest_json, "
    "snapshot_path, snapshot_sha256, state, created_by, created_at"
)

_DATASET_BY_HASH_SQL = f"""
                SELECT {_DATASET_COLUMNS} FROM transformer_training_dataset
                WHERE content_sha256 = ?
                """


class TransformerTrainingDatasetsMixin(TransformerTrainingSchemaMixin):
    """Dataset registration with immutable snapshot enforcement."""

    def _resolve_snapshot_file(self, snapshot_path: str) -> Path:
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
            raise FileNotFoundError(
                "transformer training snapshot does not exist"
            )
        return snapshot_file

    def _normalize_dataset_examples(
        self, examples: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
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
                    "owner_model_id": str(
                        example.get("owner_model_id") or ""
                    ).strip(),
                    "database_scope": scope,
                    "source_example_id": str(
                        example.get("source_example_id") or ""
                    ).strip(),
                    "source_revision": source_revision,
                    "content_sha256": item_hash,
                    "source_type": str(
                        example.get("source_type") or ""
                    ).strip(),
                    "quality_score": quality,
                }
            )
        return normalized_examples

    @staticmethod
    def _dataset_example_counts(
        normalized_examples: Sequence[Mapping[str, Any]],
    ) -> tuple[int, int]:
        train_count = sum(
            item["split"] == "train" for item in normalized_examples
        )
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
            raise ValueError(
                "dataset example ownership and provenance are required"
            )
        return train_count, validation_count

    def _insert_dataset_rows(
        self,
        connection: sqlite3.Connection,
        insert: Mapping[str, Any],
    ) -> None:
        connection.execute(
            _DATASET_INSERT_SQL,
            (
                insert["dataset_id"],
                insert["content_digest"],
                str(insert["format_version"]),
                self.BASE_MODEL_ID,
                self.RUNTIME_MODEL_ID,
                len(insert["normalized_examples"]),
                insert["train_count"],
                insert["validation_count"],
                min(
                    item["quality_score"]
                    for item in insert["normalized_examples"]
                ),
                insert["manifest_json"],
                str(insert["snapshot_file"]),
                insert["snapshot_digest"],
                str(insert["created_by"] or "star-main-native-model"),
                insert["created_at"],
            ),
        )
        connection.executemany(
            _EXAMPLE_INSERT_SQL,
            [
                (
                    insert["dataset_id"],
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
                for item in insert["normalized_examples"]
            ],
        )

    def _persist_dataset(
        self,
        connection: sqlite3.Connection,
        insert: Mapping[str, Any],
    ) -> tuple[Any, bool]:
        existing = connection.execute(
            _DATASET_BY_HASH_SQL, (insert["content_digest"],)
        ).fetchone()
        if existing is not None:
            return existing, False
        self._insert_dataset_rows(connection, insert)
        self._append_audit(
            connection,
            event_type="dataset-created",
            entity_type="training-dataset",
            entity_id=insert["dataset_id"],
            payload={
                "content_sha256": insert["content_digest"],
                "snapshot_sha256": insert["snapshot_digest"],
                "example_count": len(insert["normalized_examples"]),
                "training_example_count": insert["train_count"],
                "validation_example_count": insert["validation_count"],
                "created_by": str(insert["created_by"]),
            },
        )
        row = connection.execute(
            f"SELECT {_DATASET_COLUMNS} FROM transformer_training_dataset WHERE dataset_id = ?",
            (insert["dataset_id"],),
        ).fetchone()
        return row, True

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
        snapshot_file = self._resolve_snapshot_file(snapshot_path)
        actual_snapshot_digest = self._sha256_file(snapshot_file)
        if actual_snapshot_digest != snapshot_digest:
            raise ValueError("transformer training snapshot SHA-256 mismatch")
        normalized_examples = self._normalize_dataset_examples(examples)
        train_count, validation_count = self._dataset_example_counts(
            normalized_examples
        )
        insert = {
            "dataset_id": f"star-transformer-dataset-{content_digest[:24]}",
            "content_digest": content_digest,
            "format_version": format_version,
            "train_count": train_count,
            "validation_count": validation_count,
            "normalized_examples": normalized_examples,
            "manifest_json": self._canonical_json(dict(source_manifest)),
            "snapshot_file": snapshot_file,
            "snapshot_digest": snapshot_digest,
            "created_by": created_by,
            "created_at": self._now(),
        }
        with self._connect() as connection:
            row, inserted = self._persist_dataset(connection, insert)
        if row is None:
            raise RuntimeError("transformer training dataset was not created")
        return {**dict(row), "inserted": inserted}
