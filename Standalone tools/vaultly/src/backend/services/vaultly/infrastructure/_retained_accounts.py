from __future__ import annotations

from typing import Any, Iterable

from ._helpers import _utc_now


class RetainedAccountsMixin:
    def list_retained_account_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT account_id
                FROM vaultly_retained_accounts
                WHERE is_active = 1
                ORDER BY account_id
                """
            ).fetchall()
        return [str(row["account_id"]) for row in rows]

    def add_retained_accounts(self, account_ids: Iterable[str]) -> int:
        ids = sorted({str(item).strip() for item in account_ids if str(item).strip()})
        if not ids:
            return 0
        changed = 0
        now = _utc_now()
        with self._connect() as connection:
            for account_id in ids:
                existing = self._row_by_key(
                    connection,
                    "vaultly_retained_accounts",
                    "account_id",
                    account_id,
                )
                if existing is not None and bool(existing["is_active"]):
                    continue
                self._record_row_history(
                    connection,
                    "retained_account",
                    account_id,
                    "superseded",
                    existing,
                )
                connection.execute(
                    """
                    INSERT INTO vaultly_retained_accounts (
                        account_id, created_at, is_active, deactivated_at
                    )
                    VALUES (?, ?, 1, '')
                    ON CONFLICT(account_id) DO UPDATE SET
                        is_active = 1,
                        deactivated_at = ''
                    """,
                    (account_id, now),
                )
                self._record_row_history(
                    connection,
                    "retained_account",
                    account_id,
                    "created" if existing is None else "reactivated",
                    self._row_by_key(
                        connection,
                        "vaultly_retained_accounts",
                        "account_id",
                        account_id,
                    ),
                )
                changed += 1
        return changed

    def remove_retained_accounts(self, account_ids: Iterable[str]) -> int:
        ids = sorted({str(item).strip() for item in account_ids if str(item).strip()})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        now = _utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT account_id, created_at, is_active, deactivated_at FROM vaultly_retained_accounts
                WHERE account_id IN ({placeholders}) AND is_active = 1
                """,
                tuple(ids),
            ).fetchall()
            for row in rows:
                self._record_row_history(
                    connection,
                    "retained_account",
                    str(row["account_id"]),
                    "superseded",
                    row,
                )
            cursor = connection.execute(
                f"""
                UPDATE vaultly_retained_accounts
                SET is_active = 0, deactivated_at = ?
                WHERE account_id IN ({placeholders}) AND is_active = 1
                """,
                (now, *ids),
            )
            for row in rows:
                account_id = str(row["account_id"])
                self._record_row_history(
                    connection,
                    "retained_account",
                    account_id,
                    "deactivated",
                    self._row_by_key(
                        connection,
                        "vaultly_retained_accounts",
                        "account_id",
                        account_id,
                    ),
                )
        return cursor.rowcount
