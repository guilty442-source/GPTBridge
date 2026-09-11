from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from ._helpers import _utc_now


class PostMixin:
    @staticmethod
    def post_id_for(platform: str, post_url: str) -> str:
        digest = hashlib.sha256(f"{platform}|{post_url}".encode("utf-8")).hexdigest()
        return digest[:24]

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
            existing_full = self._row_by_key(
                connection,
                "vaultly_posts",
                "post_id",
                post_id,
            )
            self._record_row_history(
                connection,
                "post",
                post_id,
                "superseded",
                existing_full,
            )
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
            self._record_row_history(
                connection,
                "post",
                post_id,
                "created" if existing_full is None else "updated",
                self._row_by_key(
                    connection,
                    "vaultly_posts",
                    "post_id",
                    post_id,
                ),
            )

            if media_items is not None:
                previous_media = connection.execute(
                    """
                    SELECT media_id, post_id, media_index, media_type, source_url, thumbnail_url, fallback_urls_json, delivery, created_at, updated_at, is_active, deactivated_at FROM vaultly_post_media
                    WHERE post_id = ? AND is_active = 1
                    """,
                    (post_id,),
                ).fetchall()
                for previous in previous_media:
                    media_key = str(previous["media_id"])
                    self._record_row_history(
                        connection,
                        "post_media",
                        media_key,
                        "superseded",
                        previous,
                    )
                connection.execute(
                    """
                    UPDATE vaultly_post_media
                    SET is_active = 0, deactivated_at = ?, updated_at = ?
                    WHERE post_id = ? AND is_active = 1
                    """,
                    (now, now, post_id),
                )
                for previous in previous_media:
                    media_key = str(previous["media_id"])
                    self._record_row_history(
                        connection,
                        "post_media",
                        media_key,
                        "deactivated",
                        self._row_by_key(
                            connection,
                            "vaultly_post_media",
                            "media_id",
                            media_key,
                        ),
                    )
                for media_index, media in enumerate(media_items):
                    source_url = str(media.get("source_url", "")).strip()
                    media_id = hashlib.sha256(
                        f"{post_id}|{media_index}|{source_url}".encode("utf-8")
                    ).hexdigest()[:32]
                    fallback_urls = media.get("fallback_urls", [])
                    if not isinstance(fallback_urls, list):
                        fallback_urls = []
                    existing_media = self._row_by_key(
                        connection,
                        "vaultly_post_media",
                        "media_id",
                        media_id,
                    )
                    connection.execute(
                        """
                        INSERT INTO vaultly_post_media (
                            media_id, post_id, media_index, media_type, source_url,
                            thumbnail_url, fallback_urls_json, delivery, created_at, updated_at,
                            is_active, deactivated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, '')
                        ON CONFLICT(media_id) DO UPDATE SET
                            post_id = excluded.post_id,
                            media_index = excluded.media_index,
                            media_type = excluded.media_type,
                            source_url = excluded.source_url,
                            thumbnail_url = excluded.thumbnail_url,
                            fallback_urls_json = excluded.fallback_urls_json,
                            delivery = excluded.delivery,
                            updated_at = excluded.updated_at,
                            is_active = 1,
                            deactivated_at = ''
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
                    self._record_row_history(
                        connection,
                        "post_media",
                        media_id,
                        (
                            "created"
                            if existing_media is None
                            else "reactivated"
                            if not bool(existing_media["is_active"])
                            else "updated"
                        ),
                        self._row_by_key(
                            connection,
                            "vaultly_post_media",
                            "media_id",
                            media_id,
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
                    WHERE post_id IN ({placeholders}) AND is_active = 1
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
