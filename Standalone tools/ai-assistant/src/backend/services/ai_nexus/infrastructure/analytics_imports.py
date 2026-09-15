from __future__ import annotations

import re
import uuid
from typing import Any

from .analytics_common import (
    _decoded_json,
    _json,
    protect_text,
    unprotect_text,
    utc_text,
)


_IMPORT_OPERATION_STATUSES = {
    "queued",
    "processing",
    "resume_pending",
    "completed",
    "failed",
}

_UPDATE_IMPORT_OPERATION_SQL = """
UPDATE import_operations
SET import_fingerprint=?, status=?, result_encrypted=?,
    error_encrypted=?, history_encrypted=?,
    attempt_count=attempt_count+?, updated_at=?,
    started_at=?, finished_at=?
WHERE operation_id=?
"""


class ImportOperationsMixin:
    """Import operation tracking and resumability methods."""

    @staticmethod
    def _public_import_operation(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["payload"] = _decoded_json(
            unprotect_text(str(item.pop("payload_encrypted", "") or "")),
            {},
        )
        item["result"] = _decoded_json(
            unprotect_text(str(item.pop("result_encrypted", "") or "")),
            {},
        )
        item["error"] = unprotect_text(
            str(item.pop("error_encrypted", "") or "")
        )
        item["history"] = _decoded_json(
            unprotect_text(str(item.pop("history_encrypted", "") or "")),
            [],
        )
        return item

    def get_import_operation(self, operation_id: str) -> dict[str, Any] | None:
        normalized = str(operation_id or "").strip()
        if not normalized:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations WHERE operation_id = ?",
                (normalized,),
            ).fetchone()
        return self._public_import_operation(row) if row is not None else None

    def create_or_resume_import_operation(
        self,
        request_fingerprint: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        fingerprint = str(request_fingerprint or "").strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
            raise ValueError("invalid import request fingerprint")
        now = utc_text()
        with self.connect() as connection:
            row = connection.execute(
                "SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                self._insert_import_operation(
                    connection, fingerprint, payload, now
                )
            elif str(row["status"] or "") == "failed":
                self._requeue_failed_import_operation(
                    connection, row, payload, now
                )
            operation_row = connection.execute(
                "SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if operation_row is None:
            raise RuntimeError("unable to persist import operation")
        return self._public_import_operation(operation_row)

    def _insert_import_operation(
        self,
        connection: Any,
        fingerprint: str,
        payload: dict[str, Any],
        now: str,
    ) -> None:
        operation_id = uuid.uuid4().hex
        history = [
            {
                "status": "queued",
                "occurred_at": now,
                "reason": "request_created",
            }
        ]
        connection.execute(
            """
            INSERT INTO import_operations(
                operation_id, request_fingerprint, import_fingerprint,
                status, payload_encrypted, result_encrypted,
                error_encrypted, history_encrypted, attempt_count,
                created_at, updated_at, started_at, finished_at
            ) VALUES(?, ?, '', 'queued', ?, '', '', ?, 0, ?, ?, '', '')
            """,
            (
                operation_id,
                fingerprint,
                protect_text(_json(payload)),
                protect_text(_json(history)),
                now,
                now,
            ),
        )

    def _requeue_failed_import_operation(
        self,
        connection: Any,
        row: Any,
        payload: dict[str, Any],
        now: str,
    ) -> None:
        history = _decoded_json(
            unprotect_text(str(row["history_encrypted"] or "")),
            [],
        )
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "status": "queued",
                "occurred_at": now,
                "reason": "explicit_retry",
            }
        )
        connection.execute(
            """
            UPDATE import_operations
            SET status='queued', payload_encrypted=?, result_encrypted='',
                error_encrypted='', history_encrypted=?, updated_at=?,
                finished_at=''
            WHERE operation_id=?
            """,
            (
                protect_text(_json(payload)),
                protect_text(_json(history)),
                now,
                str(row["operation_id"]),
            ),
        )

    def update_import_operation(
        self,
        operation_id: str,
        *,
        status: str,
        reason: str = "",
        import_fingerprint: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        increment_attempt: bool = False,
    ) -> dict[str, Any]:
        normalized = str(operation_id or "").strip()
        normalized_status = str(status or "").strip().casefold()
        if not normalized or normalized_status not in _IMPORT_OPERATION_STATUSES:
            raise ValueError("invalid import operation update")
        now = utc_text()
        with self.connect() as connection:
            row = self._load_import_operation(connection, normalized)
            if row is None:
                raise ValueError("import operation not found")
            current_status = str(row["status"] or "")
            if current_status == "completed" and normalized_status != "completed":
                return self._public_import_operation(row)
            history, next_import_fingerprint, next_result, next_error, started_at, finished_at = (
                self._next_import_operation_values(
                    row,
                    normalized_status,
                    reason,
                    import_fingerprint,
                    result,
                    error,
                    now,
                )
            )
            self._apply_import_operation_update(
                connection,
                normalized,
                normalized_status,
                increment_attempt,
                history,
                next_import_fingerprint,
                next_result,
                next_error,
                started_at,
                finished_at,
                now,
            )
            updated = self._load_import_operation(connection, normalized)
        if updated is None:
            raise RuntimeError("import operation disappeared after update")
        return self._public_import_operation(updated)

    def _apply_import_operation_update(
        self,
        connection: Any,
        normalized: str,
        normalized_status: str,
        increment_attempt: bool,
        history: list[Any],
        next_import_fingerprint: str,
        next_result: str,
        next_error: str,
        started_at: str,
        finished_at: str,
        now: str,
    ) -> None:
        connection.execute(
            _UPDATE_IMPORT_OPERATION_SQL,
            (
                next_import_fingerprint,
                normalized_status,
                next_result,
                next_error,
                protect_text(_json(history)),
                1 if increment_attempt else 0,
                now,
                started_at,
                finished_at,
                normalized,
            ),
        )

    def _load_import_operation(
        self, connection: Any, normalized: str
    ) -> Any:
        return connection.execute(
            "SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations WHERE operation_id = ?",
            (normalized,),
        ).fetchone()

    def _next_import_operation_values(
        self,
        row: Any,
        normalized_status: str,
        reason: str,
        import_fingerprint: str | None,
        result: dict[str, Any] | None,
        error: str | None,
        now: str,
    ) -> tuple[list[Any], str, str, str, str, str]:
        history = self._next_import_history(
            row, normalized_status, reason, now
        )
        next_import_fingerprint = (
            str(import_fingerprint or "").strip().lower()
            if import_fingerprint is not None
            else str(row["import_fingerprint"] or "")
        )
        next_result = (
            protect_text(_json(result))
            if result is not None
            else str(row["result_encrypted"] or "")
        )
        next_error = (
            protect_text(str(error or ""))
            if error is not None
            else str(row["error_encrypted"] or "")
        )
        started_at = (
            now
            if normalized_status == "processing"
            and not str(row["started_at"] or "")
            else str(row["started_at"] or "")
        )
        finished_at = (
            now
            if normalized_status in {"completed", "failed"}
            else ""
            if normalized_status in {"queued", "resume_pending"}
            else str(row["finished_at"] or "")
        )
        return (
            history,
            next_import_fingerprint,
            next_result,
            next_error,
            started_at,
            finished_at,
        )

    def _next_import_history(
        self,
        row: Any,
        normalized_status: str,
        reason: str,
        now: str,
    ) -> list[Any]:
        history = _decoded_json(
            unprotect_text(str(row["history_encrypted"] or "")),
            [],
        )
        if not isinstance(history, list):
            history = []
        history.append(
            {
                "status": normalized_status,
                "occurred_at": now,
                "reason": str(reason or ""),
            }
        )
        return history

    def resumable_import_operations(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT operation_id, request_fingerprint, import_fingerprint, status, payload_encrypted, result_encrypted, error_encrypted, history_encrypted, attempt_count, created_at, updated_at, started_at, finished_at FROM import_operations
                WHERE status IN ('queued', 'processing', 'resume_pending')
                ORDER BY created_at ASC LIMIT ?
                """,
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        return [self._public_import_operation(row) for row in rows]
