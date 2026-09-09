from __future__ import annotations

import sqlite3
from pathlib import Path


class SchemaMixin:
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

                CREATE TABLE IF NOT EXISTS vaultly_entity_history (
                    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    UNIQUE(entity_type, entity_key, version)
                );
                CREATE INDEX IF NOT EXISTS idx_vaultly_entity_history_entity
                ON vaultly_entity_history(entity_type, entity_key, version DESC);
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
            for table in (
                "vaultly_accounts",
                "vaultly_post_media",
                "vaultly_filter_terms",
                "vaultly_retained_accounts",
                "vaultly_removed_accounts",
            ):
                self._ensure_column(
                    connection,
                    table,
                    "is_active",
                    "INTEGER NOT NULL DEFAULT 1",
                )
                self._ensure_column(
                    connection,
                    table,
                    "deactivated_at",
                    "TEXT NOT NULL DEFAULT ''",
                )
            self._ensure_column(
                connection,
                "vaultly_removed_accounts",
                "restored_at",
                "TEXT NOT NULL DEFAULT ''",
            )
            self._ensure_column(
                connection,
                "vaultly_media_history",
                "revision",
                "INTEGER NOT NULL DEFAULT 1",
            )
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS idx_vaultly_accounts_active
                ON vaultly_accounts(is_active, platform, handle);
                CREATE INDEX IF NOT EXISTS idx_vaultly_post_media_active
                ON vaultly_post_media(post_id, is_active, media_index);
                CREATE INDEX IF NOT EXISTS idx_vaultly_filter_terms_active
                ON vaultly_filter_terms(is_active, term);
                CREATE INDEX IF NOT EXISTS idx_vaultly_retained_accounts_active
                ON vaultly_retained_accounts(is_active, account_id);
                CREATE INDEX IF NOT EXISTS idx_vaultly_removed_accounts_active
                ON vaultly_removed_accounts(is_active, removed_at DESC);
                """
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
