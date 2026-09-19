"""Adapter candidate → evaluation → release 治理管線。

生命週期：candidate → validated → staged → active（或 rejected / retired）。
``activate`` 是唯一能改變 ``transformer_runtime_model_state`` 的路徑，且
只寫 active/previous adapter 指標——``automatic_weight_replacement``
永遠維持 0，權重替換不自動生效。
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Mapping

from .training_repo_schema import TransformerTrainingSchemaMixin


_CANDIDATE_COLUMNS = (
    "adapter_id, job_id, dataset_id, base_model_id, adapter_format, "
    "artifact_path, artifact_sha256, metrics_json, status, created_at, "
    "updated_at"
)

_CANDIDATE_SELECT = (
    f"SELECT {_CANDIDATE_COLUMNS} FROM transformer_adapter_candidate "
    "WHERE adapter_id = ?"
)

_RELEASE_ACTIONS = frozenset({"stage", "activate", "rollback", "retire"})


class TransformerAdapterRegistryMixin(TransformerTrainingSchemaMixin):
    """Governed adapter lifecycle for the transformer training repository."""

    # ------------------------------------------------------- candidate

    def _resolve_artifact(self, artifact_path: str) -> Path:
        candidate = Path(str(artifact_path or "").strip())
        if not candidate.is_absolute():
            candidate = self.tool_root / candidate
        candidate = candidate.resolve()
        if not candidate.is_relative_to(self.tool_root):
            raise PermissionError("ADAPTER_ARTIFACT_SCOPE_DENIED")
        if not candidate.is_file():
            raise FileNotFoundError("adapter artifact does not exist")
        return candidate

    def register_adapter_candidate(
        self,
        *,
        job_id: str,
        artifact_path: str,
        metrics: Mapping[str, Any],
        adapter_format: str = "native-checkpoint",
    ) -> dict[str, Any]:
        """登錄已完成訓練任務的產出為 adapter candidate。"""
        artifact = self._resolve_artifact(artifact_path)
        artifact_sha256 = self._sha256_file(artifact)
        adapter_id = f"star-transformer-adapter-{uuid.uuid4().hex[:24]}"
        now = self._now()
        with self._connect() as connection:
            job = connection.execute(
                "SELECT job_id, dataset_id, status "
                "FROM transformer_training_job WHERE job_id = ?",
                (str(job_id),),
            ).fetchone()
            if job is None:
                raise KeyError("transformer training job does not exist")
            if str(job["status"]) != "completed":
                raise ValueError("adapter requires a completed training job")
            connection.execute(
                """
                INSERT INTO transformer_adapter_candidate(
                    adapter_id, job_id, dataset_id, base_model_id,
                    adapter_format, artifact_path, artifact_sha256,
                    metrics_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'candidate', ?, ?)
                """,
                (
                    adapter_id,
                    str(job_id),
                    str(job["dataset_id"]),
                    self.BASE_MODEL_ID,
                    str(adapter_format or "native-checkpoint"),
                    str(artifact),
                    artifact_sha256,
                    self._canonical_json(dict(metrics)),
                    now,
                    now,
                ),
            )
            self._append_audit(
                connection,
                event_type="adapter-candidate-registered",
                entity_type="adapter-candidate",
                entity_id=adapter_id,
                payload={
                    "job_id": str(job_id),
                    "dataset_id": str(job["dataset_id"]),
                    "artifact_sha256": artifact_sha256,
                    "adapter_format": str(adapter_format),
                },
            )
            row = connection.execute(_CANDIDATE_SELECT, (adapter_id,)).fetchone()
        if row is None:
            raise RuntimeError("adapter candidate was not registered")
        return dict(row)

    def adapter_candidate(self, adapter_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                _CANDIDATE_SELECT, (str(adapter_id),)
            ).fetchone()
        if row is None:
            raise KeyError("transformer adapter candidate does not exist")
        return dict(row)

    # ------------------------------------------------------- evaluation

    def record_adapter_evaluation(
        self,
        *,
        adapter_id: str,
        suite_id: str,
        baseline_metrics: Mapping[str, Any],
        adapter_metrics: Mapping[str, Any],
        comparison: Mapping[str, Any],
        quality_gates: Mapping[str, Any],
        passed: bool,
        evaluated_by: str = "star-main-native-model",
    ) -> dict[str, Any]:
        """記錄評估結果；通過品質閘門時 candidate → validated。"""
        suite_metrics = self._canonical_json(
            {
                "baseline": dict(baseline_metrics),
                "adapter": dict(adapter_metrics),
            }
        )
        evaluation_id = f"star-transformer-eval-{uuid.uuid4().hex[:24]}"
        now = self._now()
        with self._connect() as connection:
            candidate = connection.execute(
                _CANDIDATE_SELECT, (str(adapter_id),)
            ).fetchone()
            if candidate is None:
                raise KeyError("transformer adapter candidate does not exist")
            if str(candidate["status"]) not in {"candidate", "validated"}:
                raise ValueError(
                    "adapter is not evaluable in status "
                    f"{candidate['status']}"
                )
            connection.execute(
                """
                INSERT INTO transformer_adapter_evaluation(
                    evaluation_id, adapter_id, suite_id, suite_sha256,
                    baseline_metrics_json, adapter_metrics_json,
                    comparison_json, quality_gates_json, passed,
                    evaluated_by, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evaluation_id,
                    str(adapter_id),
                    str(suite_id),
                    self._sha256_text(suite_metrics),
                    self._canonical_json(dict(baseline_metrics)),
                    self._canonical_json(dict(adapter_metrics)),
                    self._canonical_json(dict(comparison)),
                    self._canonical_json(dict(quality_gates)),
                    1 if passed else 0,
                    str(evaluated_by),
                    now,
                ),
            )
            new_status = (
                "validated" if passed else str(candidate["status"])
            )
            if new_status != str(candidate["status"]):
                connection.execute(
                    "UPDATE transformer_adapter_candidate "
                    "SET status = ?, updated_at = ? WHERE adapter_id = ?",
                    (new_status, now, str(adapter_id)),
                )
            self._append_audit(
                connection,
                event_type="adapter-evaluated",
                entity_type="adapter-candidate",
                entity_id=str(adapter_id),
                payload={
                    "evaluation_id": evaluation_id,
                    "suite_id": str(suite_id),
                    "passed": bool(passed),
                    "evaluated_by": str(evaluated_by),
                    "status": new_status,
                },
            )
            row = connection.execute(
                "SELECT * FROM transformer_adapter_evaluation "
                "WHERE evaluation_id = ?",
                (evaluation_id,),
            ).fetchone()
        return dict(row)

    # ------------------------------------------------------- release

    def release_adapter(
        self,
        adapter_id: str,
        action: str,
        *,
        governed_by: str,
        reason: str,
    ) -> dict[str, Any]:
        """stage / activate / rollback / retire — 受管狀態機。"""
        normalized = str(action or "").strip().casefold()
        if normalized not in _RELEASE_ACTIONS:
            raise ValueError(f"unsupported adapter release action: {action}")
        if not str(governed_by or "").strip():
            raise ValueError("governed_by is required")
        if not str(reason or "").strip():
            raise ValueError("release reason is required")
        release_id = f"star-transformer-release-{uuid.uuid4().hex[:24]}"
        now = self._now()
        with self._connect() as connection:
            candidate = connection.execute(
                _CANDIDATE_SELECT, (str(adapter_id),)
            ).fetchone()
            if candidate is None:
                raise KeyError("transformer adapter candidate does not exist")
            status = str(candidate["status"])
            runtime = connection.execute(
                "SELECT active_adapter_id, previous_adapter_id "
                "FROM transformer_runtime_model_state WHERE singleton_id = 1"
            ).fetchone()
            active = str(runtime[0]) if runtime and runtime[0] else ""
            previous = str(runtime[1]) if runtime and runtime[1] else ""

            new_status = status
            new_active, new_previous = active, previous
            previous_adapter_id = None
            if normalized == "stage":
                if status != "validated":
                    raise ValueError("only validated adapters can be staged")
                new_status = "staged"
            elif normalized == "activate":
                if status != "staged":
                    raise ValueError("only staged adapters can be activated")
                if active:
                    connection.execute(
                        "UPDATE transformer_adapter_candidate "
                        "SET status = 'staged', updated_at = ? "
                        "WHERE adapter_id = ?",
                        (now, active),
                    )
                previous_adapter_id = active or None
                new_active, new_previous = str(adapter_id), (active or None)
                new_status = "active"
            elif normalized == "rollback":
                if not previous:
                    raise ValueError("no previous adapter to roll back to")
                if active:
                    connection.execute(
                        "UPDATE transformer_adapter_candidate "
                        "SET status = 'staged', updated_at = ? "
                        "WHERE adapter_id = ?",
                        (now, active),
                    )
                connection.execute(
                    "UPDATE transformer_adapter_candidate "
                    "SET status = 'active', updated_at = ? WHERE adapter_id = ?",
                    (now, previous),
                )
                previous_adapter_id = previous
                new_active, new_previous = previous, None
                new_status = "staged"
            else:  # retire
                if status not in {"staged", "active", "candidate"}:
                    raise ValueError(
                        f"adapter in status {status} cannot be retired"
                    )
                if status == "active":
                    if previous:
                        connection.execute(
                            "UPDATE transformer_adapter_candidate "
                            "SET status = 'active', updated_at = ? "
                            "WHERE adapter_id = ?",
                            (now, previous),
                        )
                    previous_adapter_id = previous or None
                    new_active, new_previous = (previous or None), None
                new_status = "retired"

            connection.execute(
                "UPDATE transformer_adapter_candidate "
                "SET status = ?, updated_at = ? WHERE adapter_id = ?",
                (new_status, now, str(adapter_id)),
            )
            if normalized in {"activate", "rollback", "retire"}:
                connection.execute(
                    """
                    UPDATE transformer_runtime_model_state
                    SET active_adapter_id = ?, previous_adapter_id = ?,
                        updated_at = ?
                    WHERE singleton_id = 1
                    """,
                    (new_active, new_previous, now),
                )
            connection.execute(
                """
                INSERT INTO transformer_adapter_release(
                    release_id, adapter_id, action, previous_adapter_id,
                    governed_by, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    release_id,
                    str(adapter_id),
                    normalized,
                    previous_adapter_id,
                    str(governed_by),
                    str(reason),
                    now,
                ),
            )
            self._append_audit(
                connection,
                event_type="adapter-released",
                entity_type="adapter-candidate",
                entity_id=str(adapter_id),
                payload={
                    "release_id": release_id,
                    "action": normalized,
                    "previous_adapter_id": previous_adapter_id,
                    "governed_by": str(governed_by),
                    "from_status": status,
                    "to_status": new_status,
                },
            )
            row = connection.execute(
                _CANDIDATE_SELECT, (str(adapter_id),)
            ).fetchone()
        result = dict(row)
        result["runtime_state"] = self._runtime_model_state()
        return result

    def reject_adapter(
        self, adapter_id: str, *, governed_by: str, reason: str
    ) -> dict[str, Any]:
        """候選/已驗證 adapter 的明確拒絕（非 release 動作）。"""
        if not str(governed_by or "").strip() or not str(reason or "").strip():
            raise ValueError("governed_by and reason are required")
        now = self._now()
        with self._connect() as connection:
            candidate = connection.execute(
                _CANDIDATE_SELECT, (str(adapter_id),)
            ).fetchone()
            if candidate is None:
                raise KeyError("transformer adapter candidate does not exist")
            if str(candidate["status"]) not in {"candidate", "validated"}:
                raise ValueError(
                    f"adapter in status {candidate['status']} cannot be rejected"
                )
            connection.execute(
                "UPDATE transformer_adapter_candidate "
                "SET status = 'rejected', updated_at = ? WHERE adapter_id = ?",
                (now, str(adapter_id)),
            )
            self._append_audit(
                connection,
                event_type="adapter-rejected",
                entity_type="adapter-candidate",
                entity_id=str(adapter_id),
                payload={
                    "governed_by": str(governed_by),
                    "reason": str(reason),
                },
            )
            row = connection.execute(
                _CANDIDATE_SELECT, (str(adapter_id),)
            ).fetchone()
        return dict(row)

    def _runtime_model_state(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM transformer_runtime_model_state "
                "WHERE singleton_id = 1"
            ).fetchone()
        return dict(row) if row is not None else {}


__all__ = ["TransformerAdapterRegistryMixin"]
