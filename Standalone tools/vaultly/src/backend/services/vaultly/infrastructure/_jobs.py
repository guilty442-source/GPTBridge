from __future__ import annotations

import json
import sqlite3
from typing import Any

from ._helpers import _utc_now


class JobMixin:
    def create_job(
        self,
        job_id: str,
        account_ids: list[str],
        conditions: dict[str, Any],
        destination: str,
        preview_only: bool,
    ) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO vaultly_jobs (
                    job_id, status, preview_only, destination, conditions_json,
                    account_ids_json, progress_total, message, created_at
                )
                VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    1 if preview_only else 0,
                    destination,
                    json.dumps(conditions, ensure_ascii=False),
                    json.dumps(account_ids, ensure_ascii=False),
                    len(account_ids),
                    "等待背景工作",
                    now,
                ),
            )

    def create_link_job(
        self,
        job_id: str,
        links: list[str],
        conditions: dict[str, Any],
        destination: str,
        preview_only: bool,
    ) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO vaultly_jobs (
                    job_id, status, preview_only, destination, conditions_json,
                    account_ids_json, progress_total, message, created_at
                )
                VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    1 if preview_only else 0,
                    destination,
                    json.dumps(conditions, ensure_ascii=False),
                    json.dumps(links, ensure_ascii=False),
                    len(links),
                    "連結快存工作已排程",
                    now,
                ),
            )

    def update_job(self, job_id: str, **changes: Any) -> None:
        allowed = {
            "status",
            "progress_current",
            "progress_total",
            "matched",
            "downloaded",
            "skipped",
            "failed",
            "message",
            "started_at",
            "finished_at",
        }
        normalized = {key: value for key, value in changes.items() if key in allowed}
        if not normalized:
            return
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE vaultly_jobs SET {assignments} WHERE job_id = ?",
                (*normalized.values(), job_id),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_id, status, preview_only, destination, conditions_json, account_ids_json, progress_current, progress_total, matched, downloaded, skipped, failed, message, created_at, started_at, finished_at FROM vaultly_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._job_row(row) if row is not None else None

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id, status, preview_only, destination, conditions_json, account_ids_json, progress_current, progress_total, matched, downloaded, skipped, failed, message, created_at, started_at, finished_at FROM vaultly_jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(100, limit)),),
            ).fetchall()
        return [self._job_row(row) for row in rows]

    def requeue_interrupted_jobs(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id FROM vaultly_jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
            connection.execute(
                """
                UPDATE vaultly_jobs
                SET status = 'queued', message = '主程式重新啟動，工作已重新排隊',
                    started_at = NULL, finished_at = NULL
                WHERE status = 'running'
                """
            )
        return [str(row["job_id"]) for row in rows]

    @staticmethod
    def _job_row(row: sqlite3.Row) -> dict[str, Any]:
        output = dict(row)
        output["preview_only"] = bool(output["preview_only"])
        for key in ("conditions_json", "account_ids_json"):
            target = "conditions" if key == "conditions_json" else "account_ids"
            try:
                output[target] = json.loads(output.pop(key))
            except json.JSONDecodeError:
                output[target] = {} if target == "conditions" else []
        return output
