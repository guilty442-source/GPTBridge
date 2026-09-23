from __future__ import annotations

import re
import sqlite3
import uuid
from typing import Any

from .analytics_common import (
    _decoded_json,
    _json,
    protect_text,
    unprotect_text,
    utc_text,
)

_IMPORT_OPERATION_COLUMNS = (
    "operation_id, request_fingerprint, import_fingerprint, status, "
    "payload_encrypted, result_encrypted, error_encrypted, history_encrypted, "
    "attempt_count, created_at, updated_at, started_at, finished_at"
)


class AnalyticsStoreImportsMixin:
    """Import operation tracking and resumability methods."""

    @staticmethod
    def _public_import_operation(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
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
            row = connection.execute(  # sql-ok: fixed column list constant
                f"SELECT {_IMPORT_OPERATION_COLUMNS} FROM import_operations WHERE operation_id = ?",
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
            row = connection.execute(  # sql-ok: fixed column list constant
                f"SELECT {_IMPORT_OPERATION_COLUMNS} FROM import_operations WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                self._insert_import_operation(
                    connection,
                    fingerprint,
                    payload,
                    now,
                )
            elif str(row["status"] or "") == "failed":
                self._requeue_import_operation(connection, row, payload, now)
            operation_row = connection.execute(  # sql-ok: fixed column list constant
                f"SELECT {_IMPORT_OPERATION_COLUMNS} FROM import_operations WHERE request_fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        if operation_row is None:
            raise RuntimeError("unable to persist import operation")
        return self._public_import_operation(operation_row)

    def _insert_import_operation(
        self,
        connection: sqlite3.Connection,
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

    def _requeue_import_operation(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
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
        normalized, normalized_status = self._validate_import_operation_update(
            operation_id,
            status,
        )
        now = utc_text()
        with self.connect() as connection:
            row = connection.execute(  # sql-ok: fixed column list constant
                f"SELECT {_IMPORT_OPERATION_COLUMNS} FROM import_operations WHERE operation_id = ?",
                (normalized,),
            ).fetchone()
            if row is None:
                raise ValueError("import operation not found")
            current_status = str(row["status"] or "")
            if current_status == "completed" and normalized_status != "completed":
                return self._public_import_operation(row)
            updates = self._import_operation_updates(
                row,
                normalized_status,
                reason=reason,
                import_fingerprint=import_fingerprint,
                result=result,
                error=error,
                now=now,
            )
            self._apply_import_operation_update(
                connection,
                normalized,
                normalized_status,
                updates,
                increment_attempt=increment_attempt,
                now=now,
            )
            updated = connection.execute(  # sql-ok: fixed column list constant
                f"SELECT {_IMPORT_OPERATION_COLUMNS} FROM import_operations WHERE operation_id = ?",
                (normalized,),
            ).fetchone()
        if updated is None:
            raise RuntimeError("import operation disappeared after update")
        return self._public_import_operation(updated)

    @staticmethod
    def _validate_import_operation_update(
        operation_id: str,
        status: str,
    ) -> tuple[str, str]:
        normalized = str(operation_id or "").strip()
        normalized_status = str(status or "").strip().casefold()
        allowed_statuses = {
            "queued",
            "processing",
            "resume_pending",
            "completed",
            "failed",
        }
        if not normalized or normalized_status not in allowed_statuses:
            raise ValueError("invalid import operation update")
        return normalized, normalized_status

    @staticmethod
    def _apply_import_operation_update(
        connection: sqlite3.Connection,
        normalized: str,
        normalized_status: str,
        updates: dict[str, Any],
        *,
        increment_attempt: bool,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE import_operations
            SET import_fingerprint=?, status=?, result_encrypted=?,
                error_encrypted=?, history_encrypted=?,
                attempt_count=attempt_count+?, updated_at=?,
                started_at=?, finished_at=?
            WHERE operation_id=?
            """,
            (
                updates["import_fingerprint"],
                normalized_status,
                updates["result_encrypted"],
                updates["error_encrypted"],
                updates["history_encrypted"],
                1 if increment_attempt else 0,
                now,
                updates["started_at"],
                updates["finished_at"],
                normalized,
            ),
        )

    def _import_operation_updates(
        self,
        row: sqlite3.Row,
        normalized_status: str,
        *,
        reason: str,
        import_fingerprint: str | None,
        result: dict[str, Any] | None,
        error: str | None,
        now: str,
    ) -> dict[str, Any]:
        return {
            "import_fingerprint": (
                str(import_fingerprint or "").strip().lower()
                if import_fingerprint is not None
                else str(row["import_fingerprint"] or "")
            ),
            "result_encrypted": (
                protect_text(_json(result))
                if result is not None
                else str(row["result_encrypted"] or "")
            ),
            "error_encrypted": (
                protect_text(str(error or ""))
                if error is not None
                else str(row["error_encrypted"] or "")
            ),
            "history_encrypted": protect_text(
                _json(
                    self._import_operation_history(
                        row,
                        normalized_status,
                        reason=reason,
                        now=now,
                    )
                )
            ),
            **self._import_operation_timestamps(row, normalized_status, now),
        }

    @staticmethod
    def _import_operation_timestamps(
        row: sqlite3.Row,
        normalized_status: str,
        now: str,
    ) -> dict[str, str]:
        return {
            "started_at": (
                now
                if normalized_status == "processing"
                and not str(row["started_at"] or "")
                else str(row["started_at"] or "")
            ),
            "finished_at": (
                now
                if normalized_status in {"completed", "failed"}
                else ""
                if normalized_status in {"queued", "resume_pending"}
                else str(row["finished_at"] or "")
            ),
        }

    @staticmethod
    def _import_operation_history(
        row: sqlite3.Row,
        normalized_status: str,
        *,
        reason: str,
        now: str,
    ) -> list[dict[str, Any]]:
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
            rows = connection.execute(  # sql-ok: fixed column list constant
                f"""
                SELECT {_IMPORT_OPERATION_COLUMNS} FROM import_operations
                WHERE status IN ('queued', 'processing', 'resume_pending')
                ORDER BY created_at ASC LIMIT ?
                """,
                (max(1, min(1000, int(limit))),),
            ).fetchall()
        return [self._public_import_operation(row) for row in rows]


__all__ = ['AnalyticsStoreImportsMixin']
