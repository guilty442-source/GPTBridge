from __future__ import annotations

import sqlite3
import uuid
from typing import Any, Mapping

from .training_repo_schema import TransformerTrainingSchemaMixin


_JOB_INSERT_SQL = """
            INSERT INTO transformer_training_job(
                job_id, dataset_id, base_model_id, training_method,
                configuration_json, configuration_sha256, status,
                requested_by, retry_of_job_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
            """

_JOB_UPDATE_SQL = """
            UPDATE transformer_training_job
            SET status = ?, output_path = ?, error_code = ?,
                error_message = ?, started_at = ?, completed_at = ?
            WHERE job_id = ?
            """

_JOB_SELECT_SQL = (
    "SELECT * FROM transformer_training_job WHERE job_id = ?"
)


class TransformerTrainingJobsMixin(TransformerTrainingSchemaMixin):
    """Training job creation and governed status transitions."""

    @staticmethod
    def _require_prepared_dataset(
        connection: sqlite3.Connection, normalized_dataset_id: str
    ) -> None:
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

    def _insert_training_job(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        normalized_dataset_id: str,
        configuration_json: str,
        configuration_sha256: str,
        requested_by: str,
        retry_of_job_id: str | None,
        created_at: str,
    ) -> None:
        connection.execute(
            _JOB_INSERT_SQL,
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
            self._require_prepared_dataset(connection, normalized_dataset_id)
            self._insert_training_job(
                connection,
                job_id=job_id,
                normalized_dataset_id=normalized_dataset_id,
                configuration_json=configuration_json,
                configuration_sha256=configuration_sha256,
                requested_by=requested_by,
                retry_of_job_id=retry_of_job_id,
                created_at=created_at,
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
                _JOB_SELECT_SQL, (job_id,)
            ).fetchone()
        if row is None:
            raise RuntimeError("transformer training job was not created")
        return dict(row)

    def _update_training_job(
        self,
        connection: sqlite3.Connection,
        *,
        requested_status: str,
        output_path: str,
        error_code: str,
        error_message: str,
        started_at: str,
        completed_at: str,
        job_id: str,
    ) -> None:
        connection.execute(
            _JOB_UPDATE_SQL,
            (
                requested_status,
                output_path,
                str(error_code)[:96],
                str(error_message)[:1000],
                started_at,
                completed_at,
                str(job_id),
            ),
        )

    def _apply_job_transition(
        self,
        connection: sqlite3.Connection,
        row: Any,
        requested_status: str,
        output_path: str,
        error_code: str,
        error_message: str,
    ) -> Any:
        job_id = str(row["job_id"])
        now = self._now()
        started_at = (
            now if requested_status == "training" else str(row["started_at"])
        )
        completed_at = (
            now
            if requested_status in {"completed", "failed", "cancelled"}
            else str(row["completed_at"])
        )
        self._update_training_job(
            connection,
            requested_status=requested_status,
            output_path=str(output_path or row["output_path"]),
            error_code=error_code,
            error_message=error_message,
            started_at=started_at,
            completed_at=completed_at,
            job_id=job_id,
        )
        self._append_audit(
            connection,
            event_type="training-job-transitioned",
            entity_type="training-job",
            entity_id=job_id,
            payload={
                "from": str(row["status"]),
                "to": requested_status,
                "error_code": str(error_code)[:96],
            },
        )
        return connection.execute(_JOB_SELECT_SQL, (job_id,)).fetchone()

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
                _JOB_SELECT_SQL, (str(job_id),)
            ).fetchone()
            if row is None:
                raise KeyError("transformer training job does not exist")
            current = str(row["status"])
            if requested_status not in self.JOB_TRANSITIONS[current]:
                raise ValueError(
                    f"invalid transformer training transition: "
                    f"{current} -> {requested_status}"
                )
            updated = self._apply_job_transition(
                connection,
                row,
                requested_status,
                output_path,
                error_code,
                error_message,
            )
        if updated is None:
            raise RuntimeError(
                "transformer training job transition was not stored"
            )
        return dict(updated)
