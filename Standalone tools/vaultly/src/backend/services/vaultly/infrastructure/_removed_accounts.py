from __future__ import annotations

from typing import Any, Iterable

from ._helpers import _utc_now


class RemovedAccountsMixin:
    def record_removed_accounts(
        self,
        accounts: Iterable[dict[str, Any]],
        reason: str = "",
        source: str = "automatic",
    ) -> int:
        changed = 0
        now = _utc_now()
        with self._connect() as connection:
            for account in accounts:
                account_id = str(account.get("account_id", "")).strip()
                account_source = str(account.get("filter_source", source))
                if (
                    not account_id
                    or (account.get("verified") is True and account_source != "manual")
                ):
                    continue
                existing = self._row_by_key(
                    connection,
                    "vaultly_removed_accounts",
                    "account_id",
                    account_id,
                )
                self._record_row_history(
                    connection,
                    "removed_account",
                    account_id,
                    "superseded",
                    existing,
                )
                connection.execute(
                    """
                    INSERT INTO vaultly_removed_accounts (
                        account_id, platform, handle, display_name, profile_url,
                        avatar_url, verified, reason, source, removed_at,
                        is_active, deactivated_at, restored_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '', '')
                    ON CONFLICT(account_id) DO UPDATE SET
                        platform = excluded.platform,
                        handle = excluded.handle,
                        display_name = excluded.display_name,
                        profile_url = excluded.profile_url,
                        avatar_url = excluded.avatar_url,
                        verified = excluded.verified,
                        reason = excluded.reason,
                        source = excluded.source,
                        removed_at = excluded.removed_at,
                        is_active = 1,
                        deactivated_at = '',
                        restored_at = ''
                    """,
                    (
                        account_id,
                        str(account.get("platform", "")),
                        str(account.get("handle", "")),
                        str(account.get("display_name", "")),
                        str(account.get("profile_url", "")),
                        str(account.get("avatar_url", "")),
                        1 if account.get("verified") is True else 0,
                        str(account.get("filter_reason", reason)),
                        account_source,
                        now,
                    ),
                )
                self._record_row_history(
                    connection,
                    "removed_account",
                    account_id,
                    (
                        "created"
                        if existing is None
                        else "reactivated"
                        if not bool(existing["is_active"])
                        else "updated"
                    ),
                    self._row_by_key(
                        connection,
                        "vaultly_removed_accounts",
                        "account_id",
                        account_id,
                    ),
                )
                changed += 1
        return changed

    def list_removed_accounts(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT account_id, platform, handle, display_name, profile_url,
                       avatar_url, verified, reason, source, removed_at
                FROM vaultly_removed_accounts
                WHERE is_active = 1
                ORDER BY removed_at DESC
                LIMIT ?
                """,
                (max(1, min(2000, limit)),),
            ).fetchall()
        return [
            {
                **dict(row),
                "verified": bool(row["verified"]),
            }
            for row in rows
        ]

    def restore_removed_accounts(self, account_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = sorted({str(item).strip() for item in account_ids if str(item).strip()})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT account_id, platform, handle, display_name, profile_url,
                       avatar_url, verified
                FROM vaultly_removed_accounts
                WHERE account_id IN ({placeholders}) AND is_active = 1
                """,
                tuple(ids),
            ).fetchall()
        accounts = [
            {
                **dict(row),
                "verified": bool(row["verified"]),
            }
            for row in rows
        ]
        self.upsert_accounts(accounts)
        self.clear_removed_accounts(ids)
        return accounts

    def clear_removed_accounts(self, account_ids: Iterable[str]) -> int:
        ids = sorted({str(item).strip() for item in account_ids if str(item).strip()})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        now = _utc_now()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT account_id, platform, handle, display_name, profile_url, avatar_url, verified, reason, source, removed_at, is_active, deactivated_at, restored_at FROM vaultly_removed_accounts
                WHERE account_id IN ({placeholders}) AND is_active = 1
                """,
                tuple(ids),
            ).fetchall()
            for row in rows:
                self._record_row_history(
                    connection,
                    "removed_account",
                    str(row["account_id"]),
                    "superseded",
                    row,
                )
            cursor = connection.execute(
                f"""
                UPDATE vaultly_removed_accounts
                SET is_active = 0,
                    deactivated_at = ?,
                    restored_at = ?
                WHERE account_id IN ({placeholders}) AND is_active = 1
                """,
                (now, now, *ids),
            )
            for row in rows:
                account_id = str(row["account_id"])
                self._record_row_history(
                    connection,
                    "removed_account",
                    account_id,
                    "restored",
                    self._row_by_key(
                        connection,
                        "vaultly_removed_accounts",
                        "account_id",
                        account_id,
                    ),
                )
        return cursor.rowcount
