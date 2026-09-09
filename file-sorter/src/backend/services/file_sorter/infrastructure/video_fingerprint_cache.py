"""SQLite cache for video perceptual fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .cleanup_constants import VIDEO_FINGERPRINT_CACHE_SCHEMA
from .cleanup_utils import _default_state_root


class VideoFingerprintCache:
    """Small SQLite cache keyed by a privacy-preserving canonical-path digest."""

    def __init__(self, database_path: Path | None = None) -> None:
        self.database_path = database_path or (_default_state_root() / "video-fingerprints.sqlite3")
        self._ready = False

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        if not self._ready:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS video_fingerprints (
                    path_digest TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.commit()
            self._ready = True
        return connection

    @staticmethod
    def _path_digest(path: Path) -> str:
        canonical = os.path.normcase(str(path.expanduser().resolve(strict=False)))
        return hashlib.sha256(canonical.encode("utf-8", errors="surrogatepass")).hexdigest()

    def get(self, path: Path, *, size: int, mtime_ns: int) -> dict[str, Any] | None:
        try:
            with self._connect() as connection:
                row = connection.execute(
                    """
                    SELECT payload_json
                    FROM video_fingerprints
                    WHERE path_digest = ? AND size = ? AND mtime_ns = ?
                      AND schema_version = ?
                    """,
                    (
                        self._path_digest(path),
                        int(size),
                        int(mtime_ns),
                        VIDEO_FINGERPRINT_CACHE_SCHEMA,
                    ),
                ).fetchone()
            if row is None:
                return None
            payload = json.loads(str(row[0]))
            return payload if isinstance(payload, dict) else None
        except (OSError, sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            return None

    def put(
        self,
        path: Path,
        *,
        size: int,
        mtime_ns: int,
        payload: dict[str, Any],
    ) -> None:
        try:
            payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO video_fingerprints (
                        path_digest, size, mtime_ns, schema_version, payload_json
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(path_digest) DO UPDATE SET
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        schema_version = excluded.schema_version,
                        payload_json = excluded.payload_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        self._path_digest(path),
                        int(size),
                        int(mtime_ns),
                        VIDEO_FINGERPRINT_CACHE_SCHEMA,
                        payload_json,
                    ),
                )
        except (OSError, sqlite3.Error, ValueError, TypeError):
            return
