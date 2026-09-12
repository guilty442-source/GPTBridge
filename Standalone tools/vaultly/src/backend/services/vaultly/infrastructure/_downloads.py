from __future__ import annotations

from typing import Any

from ._helpers import _utc_now


class DownloadMixin:
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
            existing = self._row_by_key(
                connection,
                "vaultly_media_history",
                "dedupe_key",
                dedupe_key,
            )
            self._record_row_history(
                connection,
                "media_history",
                dedupe_key,
                "superseded",
                existing,
            )
            revision = (
                int(existing["revision"] or 1) + 1
                if existing is not None
                else 1
            )
            connection.execute(
                """
                INSERT INTO vaultly_media_history (
                    dedupe_key, platform, account_id, post_url, source_url,
                    file_path, sha256, downloaded_at, revision
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(dedupe_key) DO UPDATE SET
                    platform = excluded.platform,
                    account_id = excluded.account_id,
                    post_url = excluded.post_url,
                    source_url = excluded.source_url,
                    file_path = excluded.file_path,
                    sha256 = excluded.sha256,
                    downloaded_at = excluded.downloaded_at,
                    revision = excluded.revision
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
                    revision,
                ),
            )
