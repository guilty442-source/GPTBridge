from __future__ import annotations

import json
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_after(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


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

                CREATE TABLE IF NOT EXISTS vaultly_posts (
                    post_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    account_handle TEXT NOT NULL DEFAULT '',
                    account_display_name TEXT NOT NULL DEFAULT '',
                    post_url TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL DEFAULT '',
                    published_at TEXT NOT NULL DEFAULT '',
                    likes_text TEXT NOT NULL DEFAULT '',
                    views_text TEXT NOT NULL DEFAULT '',
                    media_count INTEGER NOT NULL DEFAULT 0,
                    downloadable_count INTEGER NOT NULL DEFAULT 0,
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    scan_status TEXT NOT NULL DEFAULT 'discovered',
                    last_error TEXT NOT NULL DEFAULT '',
                    last_inspected_at TEXT NOT NULL DEFAULT '',
                    discovered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vaultly_posts_updated
                ON vaultly_posts(updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_vaultly_posts_account
                ON vaultly_posts(platform, account_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS vaultly_post_media (
                    media_id TEXT PRIMARY KEY,
                    post_id TEXT NOT NULL,
                    media_index INTEGER NOT NULL,
                    media_type TEXT NOT NULL,
                    source_url TEXT NOT NULL DEFAULT '',
                    thumbnail_url TEXT NOT NULL DEFAULT '',
                    fallback_urls_json TEXT NOT NULL DEFAULT '[]',
                    delivery TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(post_id) REFERENCES vaultly_posts(post_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_vaultly_post_media_post
                ON vaultly_post_media(post_id, media_index);

                CREATE TABLE IF NOT EXISTS vaultly_account_scan_schedule (
                    account_id TEXT PRIMARY KEY,
                    platform TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 50,
                    status TEXT NOT NULL DEFAULT 'idle',
                    next_scan_at TEXT NOT NULL DEFAULT '',
                    last_scan_at TEXT NOT NULL DEFAULT '',
                    last_success_at TEXT NOT NULL DEFAULT '',
                    last_seen_post_url TEXT NOT NULL DEFAULT '',
                    last_seen_published_at TEXT NOT NULL DEFAULT '',
                    consecutive_empty INTEGER NOT NULL DEFAULT 0,
                    error_count INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_vaultly_account_scan_schedule_due
                ON vaultly_account_scan_schedule(status, next_scan_at, priority DESC);

                CREATE TABLE IF NOT EXISTS vaultly_post_scan_jobs (
                    scan_job_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    account_ids_json TEXT NOT NULL,
                    limit_per_account INTEGER NOT NULL DEFAULT 12,
                    inspect_existing INTEGER NOT NULL DEFAULT 0,
                    progress_current INTEGER NOT NULL DEFAULT 0,
                    progress_total INTEGER NOT NULL DEFAULT 0,
                    discovered INTEGER NOT NULL DEFAULT 0,
                    inspected INTEGER NOT NULL DEFAULT 0,
                    skipped_existing INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_vaultly_post_scan_jobs_created
                ON vaultly_post_scan_jobs(created_at DESC);

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
            self._ensure_column(
                connection,
                "vaultly_posts",
                "last_inspected_at",
                "TEXT NOT NULL DEFAULT ''",
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

    @staticmethod
    def post_id_for(platform: str, post_url: str) -> str:
        digest = hashlib.sha256(f"{platform}|{post_url}".encode("utf-8")).hexdigest()
        return digest[:24]

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

    def upsert_post(
        self,
        account: dict[str, Any],
        post: dict[str, Any],
        media_items: list[dict[str, Any]] | None = None,
        scan_status: str = "discovered",
        last_error: str = "",
    ) -> str:
        platform = str(account.get("platform", "")).strip()
        post_url = str(post.get("post_url", "")).strip()
        if not platform or not post_url:
            return ""

        post_id = self.post_id_for(platform, post_url)
        now = _utc_now()
        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT media_count, downloadable_count, thumbnail_url, scan_status
                FROM vaultly_posts
                WHERE post_id = ?
                """,
                (post_id,),
            ).fetchone()
            if media_items is None and existing is not None:
                media_count = int(existing["media_count"])
                downloadable_count = int(existing["downloadable_count"])
                thumbnail_url = str(existing["thumbnail_url"])
                existing_status = str(existing["scan_status"])
                if existing_status in {"ready", "no_media"}:
                    scan_status = existing_status
            else:
                normalized_media = media_items or []
                media_count = len(normalized_media)
                downloadable_count = sum(
                    1 for media in normalized_media if str(media.get("source_url", "")).strip()
                )
                thumbnail_url = self._thumbnail_for_media(normalized_media)

            connection.execute(
                """
                INSERT INTO vaultly_posts (
                    post_id, platform, account_id, account_handle, account_display_name,
                    post_url, text, published_at, likes_text, views_text,
                    media_count, downloadable_count, thumbnail_url, scan_status,
                    last_error, last_inspected_at, discovered_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(post_id) DO UPDATE SET
                    account_id = excluded.account_id,
                    account_handle = excluded.account_handle,
                    account_display_name = excluded.account_display_name,
                    text = CASE
                        WHEN excluded.text <> '' THEN excluded.text
                        ELSE vaultly_posts.text
                    END,
                    published_at = CASE
                        WHEN excluded.published_at <> '' THEN excluded.published_at
                        ELSE vaultly_posts.published_at
                    END,
                    likes_text = CASE
                        WHEN excluded.likes_text <> '' THEN excluded.likes_text
                        ELSE vaultly_posts.likes_text
                    END,
                    views_text = CASE
                        WHEN excluded.views_text <> '' THEN excluded.views_text
                        ELSE vaultly_posts.views_text
                    END,
                    media_count = excluded.media_count,
                    downloadable_count = excluded.downloadable_count,
                    thumbnail_url = CASE
                        WHEN excluded.thumbnail_url <> '' THEN excluded.thumbnail_url
                        ELSE vaultly_posts.thumbnail_url
                    END,
                    scan_status = excluded.scan_status,
                    last_error = excluded.last_error,
                    last_inspected_at = CASE
                        WHEN excluded.last_inspected_at <> '' THEN excluded.last_inspected_at
                        ELSE vaultly_posts.last_inspected_at
                    END,
                    updated_at = excluded.updated_at
                """,
                (
                    post_id,
                    platform,
                    str(account.get("account_id", "")).strip(),
                    str(account.get("handle", "")).strip(),
                    str(account.get("display_name", "")).strip(),
                    post_url,
                    str(post.get("text", "")).strip(),
                    str(post.get("published_at", "")).strip(),
                    str(post.get("likes", "")).strip(),
                    str(post.get("views", "")).strip(),
                    media_count,
                    downloadable_count,
                    thumbnail_url,
                    scan_status,
                    str(last_error).strip()[:1000],
                    now if media_items is not None else "",
                    now,
                    now,
                ),
            )

            if media_items is not None:
                connection.execute(
                    "DELETE FROM vaultly_post_media WHERE post_id = ?",
                    (post_id,),
                )
                for media_index, media in enumerate(media_items):
                    source_url = str(media.get("source_url", "")).strip()
                    media_id = hashlib.sha256(
                        f"{post_id}|{media_index}|{source_url}".encode("utf-8")
                    ).hexdigest()[:32]
                    fallback_urls = media.get("fallback_urls", [])
                    if not isinstance(fallback_urls, list):
                        fallback_urls = []
                    connection.execute(
                        """
                        INSERT INTO vaultly_post_media (
                            media_id, post_id, media_index, media_type, source_url,
                            thumbnail_url, fallback_urls_json, delivery, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            media_id,
                            post_id,
                            media_index,
                            str(media.get("media_type", "")).strip(),
                            source_url,
                            str(media.get("thumbnail_url", "")).strip(),
                            json.dumps([str(url) for url in fallback_urls], ensure_ascii=False),
                            str(media.get("delivery", "")).strip(),
                            now,
                            now,
                        ),
                    )
        return post_id

    @staticmethod
    def _thumbnail_for_media(media_items: list[dict[str, Any]]) -> str:
        for media in media_items:
            thumbnail_url = str(media.get("thumbnail_url", "")).strip()
            if thumbnail_url:
                return thumbnail_url
        for media in media_items:
            if str(media.get("media_type", "")).strip() == "photo":
                source_url = str(media.get("source_url", "")).strip()
                if source_url:
                    return source_url
        return ""

    @staticmethod
    def _post_query_filters(
        platform: str = "",
        status: str = "",
        query: str = "",
    ) -> tuple[str, list[Any]]:
        filters: list[str] = []
        params: list[Any] = []
        if platform and platform != "all":
            filters.append("platform = ?")
            params.append(platform)
        if status and status != "all":
            filters.append("scan_status = ?")
            params.append(status)
        if query:
            filters.append(
                """
                (
                    account_handle LIKE ?
                    OR account_display_name LIKE ?
                    OR text LIKE ?
                    OR post_url LIKE ?
                    OR platform LIKE ?
                )
                """
            )
            like_value = f"%{query}%"
            params.extend([like_value] * 5)
        where_sql = f"WHERE {' AND '.join(filters)}" if filters else ""
        return where_sql, params

    def count_posts(
        self,
        platform: str = "",
        status: str = "",
        query: str = "",
    ) -> int:
        where_sql, params = self._post_query_filters(platform, status, query)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT COUNT(*) AS total FROM vaultly_posts {where_sql}",
                params,
            ).fetchone()
        return int(row["total"] if row is not None else 0)

    def list_posts(
        self,
        limit: int = 80,
        offset: int = 0,
        platform: str = "",
        status: str = "",
        query: str = "",
    ) -> list[dict[str, Any]]:
        where_sql, params = self._post_query_filters(platform, status, query)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT post_id, platform, account_id, account_handle,
                       account_display_name, post_url, text, published_at,
                       likes_text, views_text, media_count, downloadable_count,
                       thumbnail_url, scan_status, last_error, last_inspected_at,
                       discovered_at, updated_at
                FROM vaultly_posts
                {where_sql}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (*params, max(1, min(300, limit)), max(0, offset)),
            ).fetchall()
            post_ids = [str(row["post_id"]) for row in rows]
            media_by_post: dict[str, list[dict[str, Any]]] = {post_id: [] for post_id in post_ids}
            if post_ids:
                placeholders = ",".join("?" for _ in post_ids)
                media_rows = connection.execute(
                    f"""
                    SELECT post_id, media_id, media_index, media_type, source_url,
                           thumbnail_url, fallback_urls_json, delivery
                    FROM vaultly_post_media
                    WHERE post_id IN ({placeholders})
                    ORDER BY post_id, media_index
                    """,
                    tuple(post_ids),
                ).fetchall()
                for media_row in media_rows:
                    media = dict(media_row)
                    try:
                        media["fallback_urls"] = json.loads(
                            str(media.pop("fallback_urls_json"))
                        )
                    except json.JSONDecodeError:
                        media["fallback_urls"] = []
                    media_by_post.setdefault(str(media["post_id"]), []).append(media)

            history_rows = connection.execute(
                """
                SELECT post_url, COUNT(*) AS downloaded_count
                FROM vaultly_media_history
                GROUP BY post_url
                """
            ).fetchall()
        downloaded_counts = {
            str(row["post_url"]): int(row["downloaded_count"])
            for row in history_rows
        }
        return [
            {
                **dict(row),
                "downloaded_count": downloaded_counts.get(str(row["post_url"]), 0),
                "media": media_by_post.get(str(row["post_id"]), []),
            }
            for row in rows
        ]

    def get_post_by_url(self, platform: str, post_url: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT post_id, platform, account_id, post_url, published_at,
                       scan_status, last_inspected_at, updated_at
                FROM vaultly_posts
                WHERE platform = ? AND post_url = ?
                """,
                (platform, post_url),
            ).fetchone()
        return dict(row) if row is not None else None

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
            connection.execute(
                """
                UPDATE vaultly_account_scan_schedule
                SET priority = CASE
                    WHEN account_id IN (
                        SELECT account_id FROM vaultly_accounts WHERE selected = 1
                    ) THEN 90
                    ELSE MIN(priority, 50)
                END,
                updated_at = ?
                """,
                (_utc_now(),),
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
                WHERE account_id IN ({placeholders})
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
                LEFT JOIN vaultly_accounts AS accounts
                    ON accounts.account_id = schedule.account_id
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
