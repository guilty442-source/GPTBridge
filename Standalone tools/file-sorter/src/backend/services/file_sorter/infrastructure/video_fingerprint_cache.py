"""PostgreSQL cache for video perceptual fingerprints.

A610/A621: PostgreSQL is the sole structured-data authority; this cache
lives in the bounded, non-canonical ``gptbridge_file_sorter`` schema.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared_layer.local import pg_adapter

from .cleanup_constants import VIDEO_FINGERPRINT_CACHE_SCHEMA
from .cleanup_utils import _default_state_root

PG_SCHEMA = "gptbridge_file_sorter"


class VideoFingerprintCache:
    """Small cache keyed by a privacy-preserving canonical-path digest."""

    def __init__(self, database_path: Path | None = None) -> None:
        # Legacy callers pass a filesystem path; accepted for signature
        # parity and ignored — the store is the tool-private PG schema.
        self.database_path = f"postgresql:{PG_SCHEMA}"
        self._ready = False

    def _connect(self) -> Any:
        connection = pg_adapter.connect(PG_SCHEMA)
        if not self._ready:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS video_fingerprints (
                    path_digest TEXT PRIMARY KEY,
                    size BIGINT NOT NULL,
                    mtime_ns BIGINT NOT NULL,
                    schema_version INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
        except Exception:
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
                        path_digest, size, mtime_ns, schema_version, payload_json, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path_digest) DO UPDATE SET
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        schema_version = excluded.schema_version,
                        payload_json = excluded.payload_json,
                        updated_at = excluded.updated_at
                    """,
                    (
                        self._path_digest(path),
                        int(size),
                        int(mtime_ns),
                        VIDEO_FINGERPRINT_CACHE_SCHEMA,
                        payload_json,
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    ),
                )
        except Exception:
            return
