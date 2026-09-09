from __future__ import annotations

from typing import Any, Iterable

from ._helpers import _utc_after, _utc_now


class SelectionMixin:
    def save_selection(self, account_ids: Iterable[str]) -> None:
        selected = {str(item).strip() for item in account_ids if str(item).strip()}
        now = _utc_now()
        with self._connect() as connection:
            previous_rows = connection.execute(
                """
                SELECT *
                FROM vaultly_accounts
                WHERE is_active = 1
                """
            ).fetchall()
            changed_rows = [
                row
                for row in previous_rows
                if bool(row["selected"]) != (str(row["account_id"]) in selected)
            ]
            for row in changed_rows:
                self._record_row_history(
                    connection,
                    "account",
                    str(row["account_id"]),
                    "superseded",
                    row,
                )
            connection.execute(
                "UPDATE vaultly_accounts SET selected = 0 WHERE is_active = 1"
            )
            if selected:
                placeholders = ",".join("?" for _ in selected)
                connection.execute(
                    f"""
                    UPDATE vaultly_accounts
                    SET selected = 1
                    WHERE account_id IN ({placeholders}) AND is_active = 1
                    """,
                    tuple(sorted(selected)),
                )
            for row in changed_rows:
                account_id = str(row["account_id"])
                self._record_row_history(
                    connection,
                    "account",
                    account_id,
                    "selection_updated",
                    self._row_by_key(
                        connection,
                        "vaultly_accounts",
                        "account_id",
                        account_id,
                    ),
                )
            connection.execute(
                """
                UPDATE vaultly_account_scan_schedule
                SET priority = CASE
                    WHEN account_id IN (
                        SELECT account_id
                        FROM vaultly_accounts
                        WHERE selected = 1 AND is_active = 1
                    ) THEN 90
                    ELSE MIN(priority, 50)
                END,
                updated_at = ?
                """,
                (now,),
            )

    def queue_account_scans(self, account_ids: Iterable[str], priority: int = 90) -> int:
        ids = sorted({str(item).strip() for item in account_ids if str(item).strip()})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        now = _utc_now()
        with self._connect() as connection:
            accounts = connection.execute(
                f"""
                SELECT account_id, platform, selected
                FROM vaultly_accounts
                WHERE account_id IN ({placeholders}) AND is_active = 1
                """,
                tuple(ids),
            ).fetchall()
            for account in accounts:
                self._ensure_account_scan_schedule(
                    connection,
                    str(account["account_id"]),
                    str(account["platform"]),
                    bool(account["selected"]),
                )
            cursor = connection.execute(
                f"""
                UPDATE vaultly_account_scan_schedule
                SET status = 'queued',
                    priority = MAX(priority, ?),
                    next_scan_at = ?,
                    message = '等待貼文索引',
                    updated_at = ?
                WHERE account_id IN ({placeholders})
                """,
                (max(1, min(100, priority)), now, now, *ids),
            )
        return cursor.rowcount

    def list_account_scan_schedule(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT schedule.account_id, schedule.platform, schedule.priority,
                       schedule.status, schedule.next_scan_at, schedule.last_scan_at,
                       schedule.last_success_at, schedule.last_seen_post_url,
                       schedule.last_seen_published_at, schedule.consecutive_empty,
                       schedule.error_count, schedule.message, schedule.updated_at,
                       accounts.handle, accounts.display_name, accounts.selected
                FROM vaultly_account_scan_schedule AS schedule
                INNER JOIN vaultly_accounts AS accounts
                    ON accounts.account_id = schedule.account_id
                   AND accounts.is_active = 1
                ORDER BY accounts.selected DESC, schedule.priority DESC,
                         schedule.next_scan_at ASC, accounts.handle COLLATE NOCASE
                LIMIT ?
                """,
                (max(1, min(1000, limit)),),
            ).fetchall()
        return [
            {
                **dict(row),
                "selected": bool(row["selected"]),
            }
            for row in rows
        ]

    def get_account_scan_schedule(self, account_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT account_id, platform, priority, status, next_scan_at,
                       last_scan_at, last_success_at, last_seen_post_url,
                       last_seen_published_at, consecutive_empty, error_count,
                       message, updated_at
                FROM vaultly_account_scan_schedule
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def mark_account_scan_started(self, account_id: str) -> None:
        now = _utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE vaultly_account_scan_schedule
                SET status = 'scanning',
                    last_scan_at = ?,
                    message = '正在索引貼文',
                    updated_at = ?
                WHERE account_id = ?
                """,
                (now, now, account_id),
            )

    def mark_account_scan_success(
        self,
        account_id: str,
        newest_post_url: str,
        newest_published_at: str,
        discovered_count: int,
        inspected_count: int,
    ) -> None:
        now = _utc_now()
        if inspected_count > 0 or discovered_count > 0:
            next_scan_at = _utc_after(30 * 60)
            consecutive_empty = 0
        else:
            next_scan_at = _utc_after(6 * 60 * 60)
            consecutive_empty = 1
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE vaultly_account_scan_schedule
                SET status = 'idle',
                    next_scan_at = ?,
                    last_success_at = ?,
                    last_seen_post_url = CASE
                        WHEN ? <> '' THEN ?
                        ELSE last_seen_post_url
                    END,
                    last_seen_published_at = CASE
                        WHEN ? <> '' THEN ?
                        ELSE last_seen_published_at
                    END,
                    consecutive_empty = CASE
                        WHEN ? > 0 THEN 0
                        ELSE consecutive_empty + ?
                    END,
                    error_count = 0,
                    message = ?,
                    updated_at = ?
                WHERE account_id = ?
                """,
                (
                    next_scan_at,
                    now,
                    newest_post_url,
                    newest_post_url,
                    newest_published_at,
                    newest_published_at,
                    discovered_count,
                    consecutive_empty,
                    f"索引完成：新增/檢查 {inspected_count} 篇",
                    now,
                    account_id,
                ),
            )

    def mark_account_scan_failure(self, account_id: str, message: str) -> None:
        now = _utc_now()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT error_count
                FROM vaultly_account_scan_schedule
                WHERE account_id = ?
                """,
                (account_id,),
            ).fetchone()
            error_count = int(row["error_count"] if row is not None else 0) + 1
            delay_seconds = 5 * 60 if error_count == 1 else 30 * 60 if error_count == 2 else 6 * 60 * 60
            connection.execute(
                """
                UPDATE vaultly_account_scan_schedule
                SET status = 'error',
                    next_scan_at = ?,
                    error_count = ?,
                    message = ?,
                    updated_at = ?
                WHERE account_id = ?
                """,
                (
                    _utc_after(delay_seconds),
                    error_count,
                    message[:500],
                    now,
                    account_id,
                ),
            )
