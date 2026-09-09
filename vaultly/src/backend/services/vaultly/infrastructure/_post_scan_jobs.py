from __future__ import annotations

import json
import sqlite3
from typing import Any

from ._helpers import _utc_now


class PostScanJobMixin:
    def create_post_scan_job(
        self,
        scan_job_id: str,
        account_ids: list[str],
        limit_per_account: int,
        inspect_existing: bool,
    ) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO vaultly_post_scan_jobs (
                    scan_job_id, status, account_ids_json, limit_per_account,
                    inspect_existing, progress_total, message, created_at
                )
                VALUES (?, 'queued', ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_job_id,
                    json.dumps(account_ids, ensure_ascii=False),
                    limit_per_account,
                    1 if inspect_existing else 0,
                    len(account_ids),
                    "等待貼文索引",
                    now,
                ),
            )

    def update_post_scan_job(self, scan_job_id: str, **changes: Any) -> None:
        allowed = {
            "status",
            "progress_current",
            "progress_total",
            "discovered",
            "inspected",
            "skipped_existing",
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
                f"UPDATE vaultly_post_scan_jobs SET {assignments} WHERE scan_job_id = ?",
                (*normalized.values(), scan_job_id),
            )

    def get_post_scan_job(self, scan_job_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM vaultly_post_scan_jobs WHERE scan_job_id = ?",
                (scan_job_id,),
            ).fetchone()
        return self._post_scan_job_row(row) if row is not None else None

    def list_post_scan_jobs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM vaultly_post_scan_jobs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (max(1, min(50, limit)),),
            ).fetchall()
        return [self._post_scan_job_row(row) for row in rows]

    def requeue_interrupted_post_scan_jobs(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT scan_job_id
                FROM vaultly_post_scan_jobs
                WHERE status IN ('queued', 'running')
                """
            ).fetchall()
            connection.execute(
                """
                UPDATE vaultly_post_scan_jobs
                SET status = 'queued',
                    message = '上次中斷，等待重新索引',
                    started_at = NULL,
                    finished_at = NULL
                WHERE status = 'running'
                """
            )
        return [str(row["scan_job_id"]) for row in rows]

    @staticmethod
    def _post_scan_job_row(row: sqlite3.Row) -> dict[str, Any]:
        output = dict(row)
        output["inspect_existing"] = bool(output["inspect_existing"])
        try:
            output["account_ids"] = json.loads(output.pop("account_ids_json"))
        except json.JSONDecodeError:
            output["account_ids"] = []
        return output
