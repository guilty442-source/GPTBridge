from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from ._helpers import _utc_now


class AccountMixin:
    def upsert_accounts(self, accounts: Iterable[dict[str, Any]]) -> int:
        now = _utc_now()
        changed = 0
        with self._connect() as connection:
            for account in accounts:
                account_id = str(account.get("account_id", "")).strip()
                if not account_id:
                    continue
                existing = self._row_by_key(
                    connection,
                    "vaultly_accounts",
                    "account_id",
                    account_id,
                )
                self._record_row_history(
                    connection,
                    "account",
                    account_id,
                    "superseded",
                    existing,
                )
                connection.execute(
                    """
                    INSERT INTO vaultly_accounts (
                        account_id, platform, handle, display_name, profile_url,
                        avatar_url, verified, selected, discovered_at, updated_at,
                        is_active, deactivated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 1, '')
                    ON CONFLICT(account_id) DO UPDATE SET
                        platform = CASE
                            WHEN excluded.platform <> '' THEN excluded.platform
                            ELSE vaultly_accounts.platform
                        END,
                        handle = CASE
                            WHEN excluded.handle <> '' THEN excluded.handle
                            ELSE vaultly_accounts.handle
                        END,
                        display_name = CASE
                            WHEN excluded.display_name <> '' THEN excluded.display_name
                            ELSE vaultly_accounts.display_name
                        END,
                        profile_url = excluded.profile_url,
                        avatar_url = CASE
                            WHEN excluded.avatar_url <> '' THEN excluded.avatar_url
                            ELSE vaultly_accounts.avatar_url
                        END,
                        verified = MAX(vaultly_accounts.verified, excluded.verified),
                        is_active = 1,
                        deactivated_at = '',
                        updated_at = excluded.updated_at
                    """,
                    (
                        account_id,
                        str(account.get("platform", "")),
                        str(account.get("handle", "")),
                        str(account.get("display_name", "")),
                        str(account.get("profile_url", "")),
                        str(account.get("avatar_url", "")),
                        1 if account.get("verified") is True else 0,
                        now,
                        now,
                    ),
                )
                current = self._row_by_key(
                    connection,
                    "vaultly_accounts",
                    "account_id",
                    account_id,
                )
                self._record_row_history(
                    connection,
                    "account",
                    account_id,
                    (
                        "created"
                        if existing is None
                        else "reactivated"
                        if not bool(existing["is_active"])
                        else "updated"
                    ),
                    current,
                )
                self._ensure_account_scan_schedule(
                    connection,
                    account_id,
                    str(account.get("platform", "")),
                    selected=False,
                )
                changed += 1
        return changed

    def _ensure_account_scan_schedule(
        self,
        connection: sqlite3.Connection,
        account_id: str,
        platform: str,
        selected: bool,
    ) -> None:
        now = _utc_now()
        priority = 90 if selected else 50
        connection.execute(
            """
            INSERT INTO vaultly_account_scan_schedule (
                account_id, platform, priority, status, next_scan_at, updated_at
            )
            VALUES (?, ?, ?, 'idle', ?, ?)
            ON CONFLICT(account_id) DO UPDATE SET
                platform = excluded.platform,
                priority = MAX(vaultly_account_scan_schedule.priority, excluded.priority),
                updated_at = excluded.updated_at
            """,
            (account_id, platform, priority, now, now),
        )

    def list_accounts(self, selected_only: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT account_id, platform, handle, display_name, profile_url,
                   avatar_url, verified, selected, discovered_at, updated_at
            FROM vaultly_accounts
            WHERE is_active = 1
        """
        if selected_only:
            query += " AND selected = 1"
        query += " ORDER BY platform, handle COLLATE NOCASE"
        with self._connect() as connection:
            rows = connection.execute(query).fetchall()
        return [
            {
                "account_id": row["account_id"],
                "platform": row["platform"],
                "handle": row["handle"],
                "display_name": row["display_name"],
                "profile_url": row["profile_url"],
                "avatar_url": row["avatar_url"],
                "verified": bool(row["verified"]),
                "selected": bool(row["selected"]),
                "discovered_at": row["discovered_at"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def get_accounts(self, account_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = [str(item).strip() for item in account_ids if str(item).strip()]
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT account_id, platform, handle, display_name, profile_url,
                       avatar_url, verified, selected, discovered_at, updated_at
                FROM vaultly_accounts
                WHERE account_id IN ({placeholders}) AND is_active = 1
                ORDER BY platform, handle COLLATE NOCASE
                """,
                ids,
            ).fetchall()
        return [
            {
                **dict(row),
                "verified": bool(row["verified"]),
                "selected": bool(row["selected"]),
            }
            for row in rows
        ]

    def delete_accounts(self, account_ids: Iterable[str]) -> int:
        ids = sorted({str(item).strip() for item in account_ids if str(item).strip()})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        now = _utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM vaultly_accounts
                WHERE account_id IN ({placeholders}) AND is_active = 1
                """,
                tuple(ids),
            ).fetchall()
            for row in rows:
                self._record_row_history(
                    connection,
                    "account",
                    str(row["account_id"]),
                    "superseded",
                    row,
                )
            cursor = connection.execute(
                f"""
                UPDATE vaultly_accounts
                SET is_active = 0, selected = 0, deactivated_at = ?, updated_at = ?
                WHERE account_id IN ({placeholders}) AND is_active = 1
                """,
                (now, now, *ids),
            )
            for row in rows:
                account_id = str(row["account_id"])
                self._record_row_history(
                    connection,
                    "account",
                    account_id,
                    "deactivated",
                    self._row_by_key(
                        connection,
                        "vaultly_accounts",
                        "account_id",
                        account_id,
                    ),
                )
            return cursor.rowcount
