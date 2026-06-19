from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class VaultlyRepository:
    def __init__(self, project_root: Path) -> None:
        self.db_path = project_root / "runtime" / "state" / "vaultly.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS vaultly_accounts (
                    account_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    handle TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    profile_url TEXT NOT NULL,
                    avatar_url TEXT NOT NULL DEFAULT '',
                    verified INTEGER NOT NULL DEFAULT 0,
                    selected INTEGER NOT NULL DEFAULT 0,
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vaultly_accounts_platform
                ON vaultly_accounts(platform, handle);

                CREATE TABLE IF NOT EXISTS vaultly_jobs (
                    job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    preview_only INTEGER NOT NULL DEFAULT 0,
                    destination TEXT NOT NULL DEFAULT '',
                    conditions_json TEXT NOT NULL,
                    account_ids_json TEXT NOT NULL,
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    matched INTEGER NOT NULL DEFAULT 0,
                    downloaded INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_vaultly_jobs_created
                ON vaultly_jobs(created_at DESC);

                CREATE TABLE IF NOT EXISTS vaultly_media_history (
                    dedupe_key TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    post_url TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    downloaded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vaultly_settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vaultly_filter_terms (
                    term TEXT PRIMARY KEY COLLATE NOCASE,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vaultly_retained_accounts (
                    account_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vaultly_removed_accounts (
                    account_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    handle TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    profile_url TEXT NOT NULL,
                    avatar_url TEXT NOT NULL DEFAULT '',
                    verified INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT 'automatic',
                    removed_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vaultly_removed_accounts_removed
                ON vaultly_removed_accounts(removed_at DESC);
                """
            )
            self._ensure_column(
                connection,
                "vaultly_accounts",
                "verified",
                "INTEGER NOT NULL DEFAULT 0",
            )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def upsert_accounts(self, accounts: Iterable[dict[str, Any]]) -> int:
        now = _utc_now()
        changed = 0
        with self._connect() as connection:
            for account in accounts:
                account_id = str(account.get("account_id", "")).strip()
                if not account_id:
                    continue
                connection.execute(
                    """
                    INSERT INTO vaultly_accounts (
                        account_id, platform, handle, display_name, profile_url,
                        avatar_url, verified, selected, discovered_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
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
                changed += 1
        return changed

    def list_accounts(self, selected_only: bool = False) -> list[dict[str, Any]]:
        query = """
            SELECT account_id, platform, handle, display_name, profile_url,
                   avatar_url, verified, selected, discovered_at, updated_at
            FROM vaultly_accounts
        """
        if selected_only:
            query += " WHERE selected = 1"
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
                WHERE account_id IN ({placeholders})
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

    def save_selection(self, account_ids: Iterable[str]) -> None:
        selected = {str(item).strip() for item in account_ids if str(item).strip()}
        with self._connect() as connection:
            connection.execute("UPDATE vaultly_accounts SET selected = 0")
            if selected:
                placeholders = ",".join("?" for _ in selected)
                connection.execute(
                    f"UPDATE vaultly_accounts SET selected = 1 WHERE account_id IN ({placeholders})",
                    tuple(sorted(selected)),
                )

    def delete_accounts(self, account_ids: Iterable[str]) -> int:
        ids = sorted({str(item).strip() for item in account_ids if str(item).strip()})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM vaultly_accounts WHERE account_id IN ({placeholders})",
                tuple(ids),
            )
            return cursor.rowcount

    @staticmethod
    def _normalize_filter_terms(terms: Iterable[str]) -> list[str]:
        return sorted(
            {
                str(term).strip()[:120]
                for term in terms
                if str(term).strip()
            },
            key=str.casefold,
        )

    def list_filter_terms(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT term FROM vaultly_filter_terms ORDER BY term COLLATE NOCASE"
            ).fetchall()
        return [str(row["term"]) for row in rows]

    def add_filter_terms(self, terms: Iterable[str]) -> int:
        normalized = self._normalize_filter_terms(terms)
        if not normalized:
            return 0
        changed = 0
        now = _utc_now()
        with self._connect() as connection:
            for term in normalized:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO vaultly_filter_terms (term, created_at)
                    VALUES (?, ?)
                    """,
                    (term, now),
                )
                changed += cursor.rowcount
        return changed

    def remove_filter_terms(self, terms: Iterable[str]) -> int:
        normalized = self._normalize_filter_terms(terms)
        if not normalized:
            return 0
        placeholders = ",".join("?" for _ in normalized)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM vaultly_filter_terms WHERE term IN ({placeholders})",
                tuple(normalized),
            )
        return cursor.rowcount

    def list_retained_account_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT account_id FROM vaultly_retained_accounts ORDER BY account_id"
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
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO vaultly_retained_accounts (account_id, created_at)
                    VALUES (?, ?)
                    """,
                    (account_id, now),
                )
                changed += cursor.rowcount
        return changed

    def remove_retained_accounts(self, account_ids: Iterable[str]) -> int:
        ids = sorted({str(item).strip() for item in account_ids if str(item).strip()})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM vaultly_retained_accounts WHERE account_id IN ({placeholders})",
                tuple(ids),
            )
        return cursor.rowcount

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
                connection.execute(
                    """
                    INSERT INTO vaultly_removed_accounts (
                        account_id, platform, handle, display_name, profile_url,
                        avatar_url, verified, reason, source, removed_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(account_id) DO UPDATE SET
                        display_name = excluded.display_name,
                        profile_url = excluded.profile_url,
                        avatar_url = excluded.avatar_url,
                        verified = excluded.verified,
                        reason = excluded.reason,
                        source = excluded.source,
                        removed_at = excluded.removed_at
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
                changed += 1
        return changed

    def list_removed_accounts(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT account_id, platform, handle, display_name, profile_url,
                       avatar_url, verified, reason, source, removed_at
                FROM vaultly_removed_accounts
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
                WHERE account_id IN ({placeholders})
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
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM vaultly_removed_accounts WHERE account_id IN ({placeholders})",
                tuple(ids),
            )
        return cursor.rowcount

    def set_setting(self, key: str, value: Any) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO vaultly_settings (key, value_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, json.dumps(value, ensure_ascii=False), _utc_now()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM vaultly_settings WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value_json"])
        except json.JSONDecodeError:
            return default

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
                "SELECT * FROM vaultly_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return self._job_row(row) if row is not None else None

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM vaultly_jobs ORDER BY created_at DESC LIMIT ?",
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

    def is_downloaded(self, dedupe_key: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM vaultly_media_history WHERE dedupe_key = ?",
                (dedupe_key,),
            ).fetchone()
        return row is not None

    def get_download(self, dedupe_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT dedupe_key, platform, account_id, post_url, source_url,
                       file_path, sha256, downloaded_at
                FROM vaultly_media_history
                WHERE dedupe_key = ?
                """,
                (dedupe_key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def record_download(
        self,
        dedupe_key: str,
        platform: str,
        account_id: str,
        post_url: str,
        source_url: str,
        file_path: str,
        sha256: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO vaultly_media_history (
                    dedupe_key, platform, account_id, post_url, source_url,
                    file_path, sha256, downloaded_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dedupe_key,
                    platform,
                    account_id,
                    post_url,
                    source_url,
                    file_path,
                    sha256,
                    _utc_now(),
                ),
            )
