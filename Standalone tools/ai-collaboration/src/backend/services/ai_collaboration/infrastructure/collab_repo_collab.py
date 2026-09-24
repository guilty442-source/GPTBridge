"""Persistence for COLLABORATION_TASK_CONTRACT records.

Two tables: one row per collaboration task, one row per provider reply.
Every completed provider reply is stored — never only the last one.  No
credentials, session tokens, cookies or secrets are ever written here.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from .collab_repo_constants import utc_now

TASK_STATUSES = frozenset(
    {
        "created",
        "validating",
        "running",
        "waiting_user",
        "aggregating",
        "completed",
        "partial",
        "failed",
        "cancelled",
    }
)

_TASK_COLUMNS = (
    "task_id, request_id, mode, selected_providers_json, original_request, "
    "task_generation, attempt_id, overall_status, comparison_json, "
    "synthesis_json, summary_reference, fault_reference, "
    "created_at, started_at, completed_at, updated_at"
)

_RESULT_COLUMNS = (
    "result_pk, task_id, request_id, provider_id, response_id, attempt_id, "
    "response_text, response_status, capture_method, captured_at, "
    "completion_evidence, adapter_version, content_class, "
    "suggested_actions_json, created_at"
)


class CollabRepoTasksMixin:
    """Collaboration task + per-provider result persistence."""

    # ------------------------------------------------------------------
    # tasks
    # ------------------------------------------------------------------
    def create_collab_task(
        self,
        *,
        request_id: str,
        mode: str,
        selected_providers: list[str],
        original_request: str,
        task_generation: str,
        attempt_id: str,
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:16]
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_nexus_collab_tasks
                (task_id, request_id, mode, selected_providers_json,
                 original_request, task_generation, attempt_id,
                 overall_status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'created', ?, ?)
                """,
                (
                    task_id,
                    request_id,
                    mode,
                    json.dumps(selected_providers, ensure_ascii=False),
                    original_request[:64_000],
                    task_generation,
                    attempt_id,
                    now,
                    now,
                ),
            )
        return self.get_collab_task(task_id) or {}

    def update_collab_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        request_id: str | None = None,
        attempt_id: str | None = None,
        comparison: dict[str, Any] | None = None,
        synthesis: dict[str, Any] | None = None,
        summary_reference: str | None = None,
        fault_reference: str | None = None,
        started: bool = False,
        completed: bool = False,
    ) -> None:
        if status is not None and status not in TASK_STATUSES:
            raise ValueError(f"unknown collaboration task status: {status}")
        now = utc_now()
        assignments = ["updated_at = ?"]
        params: list[Any] = [now]
        if status is not None:
            assignments.append("overall_status = ?")
            params.append(status)
        if request_id is not None:
            assignments.append("request_id = ?")
            params.append(request_id)
        if attempt_id is not None:
            assignments.append("attempt_id = ?")
            params.append(attempt_id)
        if comparison is not None:
            assignments.append("comparison_json = ?")
            params.append(json.dumps(comparison, ensure_ascii=False))
        if synthesis is not None:
            assignments.append("synthesis_json = ?")
            params.append(json.dumps(synthesis, ensure_ascii=False))
        if summary_reference is not None:
            assignments.append("summary_reference = ?")
            params.append(summary_reference)
        if fault_reference is not None:
            assignments.append("fault_reference = ?")
            params.append(fault_reference)
        if started:
            assignments.append("started_at = CASE WHEN started_at = '' THEN ? ELSE started_at END")
            params.append(now)
        if completed:
            assignments.append("completed_at = ?")
            params.append(now)
        params.append(task_id)
        with self._connect() as connection:
            connection.execute(  # sql-ok: fixed assignment list, values parameterized
                f"UPDATE ai_nexus_collab_tasks SET {', '.join(assignments)} WHERE task_id = ?",
                params,
            )

    def get_collab_task(self, task_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(  # sql-ok: fixed column list
                f"SELECT {_TASK_COLUMNS} FROM ai_nexus_collab_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        item = self._task_row(row)
        item["provider_results"] = self.list_collab_results(task_id)
        return item

    def list_collab_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(  # sql-ok: fixed column list
                f"SELECT {_TASK_COLUMNS} FROM ai_nexus_collab_tasks ORDER BY created_at DESC LIMIT ?",
                (max(1, min(200, limit)),),
            ).fetchall()
        items = [self._task_row(row) for row in rows]
        for item in items:
            item["provider_results"] = self.list_collab_results(
                str(item["task_id"])
            )
        return items

    def interrupted_collab_tasks(self, runtime_generation: str) -> list[str]:
        """Tasks left 'running' by a dead runtime get a clean tombstone so a
        later resume uses a fresh attempt identity instead of replaying."""
        now = utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id FROM ai_nexus_collab_tasks
                WHERE overall_status IN ('validating', 'running', 'aggregating')
                  AND task_generation != ?
                """,
                (runtime_generation,),
            ).fetchall()
            ids = [str(row["task_id"]) for row in rows]
            for task_id in ids:
                results = self.list_collab_results(task_id)
                has_completed = any(
                    str(item.get("response_status") or "") == "completed"
                    for item in results
                )
                connection.execute(  # sql-ok: bounded tombstone loop over interrupted task ids
                    """
                    UPDATE ai_nexus_collab_tasks
                    SET overall_status = ?, fault_reference = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (
                        "partial" if has_completed else "failed",
                        "RUNTIME_RESTART",
                        now,
                        task_id,
                    ),
                )
            return ids

    # ------------------------------------------------------------------
    # per-provider results
    # ------------------------------------------------------------------
    def upsert_collab_result(self, record: dict[str, Any]) -> None:
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_nexus_collab_results
                (result_pk, task_id, request_id, provider_id, response_id,
                 attempt_id, response_text, response_status, capture_method,
                 captured_at, completion_evidence, adapter_version,
                 content_class, suggested_actions_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id, provider_id, attempt_id) DO UPDATE SET
                    response_text = excluded.response_text,
                    response_status = excluded.response_status,
                    capture_method = excluded.capture_method,
                    captured_at = excluded.captured_at,
                    completion_evidence = excluded.completion_evidence,
                    suggested_actions_json = excluded.suggested_actions_json
                """,
                (
                    f"{record.get('task_id')}:{record.get('provider_id')}:{record.get('attempt_id')}",
                    str(record.get("task_id") or ""),
                    str(record.get("request_id") or ""),
                    str(record.get("provider_id") or ""),
                    str(record.get("response_id") or ""),
                    str(record.get("attempt_id") or ""),
                    str(record.get("response_text") or ""),
                    str(record.get("response_status") or ""),
                    str(record.get("capture_method") or ""),
                    str(record.get("captured_at") or ""),
                    str(record.get("completion_evidence") or ""),
                    str(record.get("adapter_version") or ""),
                    str(record.get("content_class") or ""),
                    json.dumps(
                        record.get("suggested_actions") or [],
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

    def list_collab_results(self, task_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(  # sql-ok: fixed column list
                f"SELECT {_RESULT_COLUMNS} FROM ai_nexus_collab_results WHERE task_id = ? ORDER BY created_at ASC",
                (task_id,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item.pop("result_pk", None)
            item["suggested_actions"] = self._json_list(
                item.pop("suggested_actions_json", "[]")
            )
            output.append(item)
        return output

    def completed_result_providers(
        self, task_id: str, attempt_id: str
    ) -> set[str]:
        """Providers that already completed on this attempt — a resume must
        never re-send their external request."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT provider_id FROM ai_nexus_collab_results
                WHERE task_id = ? AND attempt_id = ? AND response_status = 'completed'
                """,
                (task_id, attempt_id),
            ).fetchall()
        return {str(row["provider_id"]) for row in rows}

    # ------------------------------------------------------------------
    @staticmethod
    def _task_row(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["selected_providers"] = [
            str(value)
            for value in json.loads(
                str(item.pop("selected_providers_json", "[]") or "[]")
            )
        ]
        for key, column in (
            ("comparison", "comparison_json"),
            ("synthesis", "synthesis_json"),
        ):
            try:
                item[key] = json.loads(str(item.pop(column, "{}") or "{}"))
            except json.JSONDecodeError:
                item[key] = {}
        return item
