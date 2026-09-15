from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

from .analytics_common import (
    InvestmentAnalyticsUpgradeRequired,
    SCHEMA_VERSION,
    _atomic_write_bytes,
    _copy_verified,
    _portable_sqlite_image,
    _validated_storage_path,
    decode_binary_document,
    encode_binary_document,
    utc_now,
    utc_text,
)


class AnalyticsStoreConnectionMixin:
    """Connection management, durable persistence, and database loading."""

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        with self._database_lock:
            before_changes = self._database_connection.total_changes
            savepoint = (
                f"gptbridge_connect_{uuid.uuid4().hex}"
                if self._batch_depth > 0
                else ""
            )
            if savepoint:
                self._database_connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield self._database_connection
            except Exception:
                if savepoint:
                    self._database_connection.execute(
                        f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                    self._database_connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                else:
                    self._database_connection.rollback()
                raise
            else:
                changed = (
                    self._database_connection.total_changes != before_changes
                )
                if savepoint:
                    self._database_connection.execute(
                        f"RELEASE SAVEPOINT {savepoint}"
                    )
                    if changed:
                        self._pending_persist = True
                    return
                self._database_connection.commit()
                if not changed:
                    return
                try:
                    self._persist_database()
                except Exception:
                    self._restore_durable_database_image()
                    raise

    @contextmanager
    def exclusive_data_access(self) -> Iterator[None]:
        """Hold the database side of the repository -> database lock order."""

        with self._database_lock:
            yield

    @contextmanager
    def batch_updates(self) -> Iterator[None]:
        """Run nested writes as one durable all-or-nothing transaction."""

        with self._database_lock:
            outermost = self._batch_depth == 0
            savepoint = (
                ""
                if outermost
                else f"gptbridge_batch_{uuid.uuid4().hex}"
            )
            if outermost:
                self._database_connection.execute("BEGIN IMMEDIATE")
                self._pending_persist = False
            else:
                self._database_connection.execute(f"SAVEPOINT {savepoint}")
            self._batch_depth += 1
            try:
                yield
            except Exception:
                self._abort_batch(outermost=outermost, savepoint=savepoint)
                raise
            else:
                self._finish_batch(outermost=outermost, savepoint=savepoint)

    def _abort_batch(self, *, outermost: bool, savepoint: str) -> None:
        self._batch_depth = max(0, self._batch_depth - 1)
        if outermost:
            self._database_connection.rollback()
            self._pending_persist = False
        else:
            self._database_connection.execute(
                f"ROLLBACK TO SAVEPOINT {savepoint}"
            )
            self._database_connection.execute(
                f"RELEASE SAVEPOINT {savepoint}"
            )

    def _finish_batch(self, *, outermost: bool, savepoint: str) -> None:
        self._batch_depth = max(0, self._batch_depth - 1)
        if not outermost:
            self._database_connection.execute(
                f"RELEASE SAVEPOINT {savepoint}"
            )
            return
        changed = self._pending_persist
        self._pending_persist = False
        try:
            self._database_connection.commit()
        except Exception:
            self._database_connection.rollback()
            raise
        if not changed:
            return
        try:
            self._persist_database()
        except Exception:
            self._restore_durable_database_image()
            raise

    def _restore_durable_database_image(self) -> None:
        if self._durable_database_image is None:
            self._database_connection.close()
            self._database_connection = sqlite3.connect(
                ":memory:",
                check_same_thread=False,
            )
        else:
            self._database_connection.deserialize(self._durable_database_image)
        self._database_connection.row_factory = sqlite3.Row
        self._database_connection.execute("PRAGMA foreign_keys = ON")
        self._database_connection.execute("PRAGMA busy_timeout = 10000")

    def _loaded_schema_version(self) -> int:
        try:
            table = self._database_connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='metadata'"
            ).fetchone()
            if table is None:
                return 0
            row = self._database_connection.execute(
                "SELECT value FROM metadata WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                return 0
            value = str(row[0]).strip()
            if not value.isdigit():
                raise ValueError("investment analytics schema version is invalid")
            version = int(value)
            if version < 1:
                raise ValueError("investment analytics schema version is invalid")
            return version
        except sqlite3.DatabaseError as exc:
            raise ValueError(
                "investment analytics schema metadata cannot be read"
            ) from exc

    def _preserve_pre_schema_migration(
        self,
        database_bytes: bytes,
        *,
        previous_schema_version: int,
    ) -> None:
        self.prune_backups(remove_all=True)
        stamp = utc_now().strftime("%Y%m%dT%H%M%S")
        backup_id = uuid.uuid4().hex
        stem = f"{stamp}-{backup_id[:8]}"
        path = self.backup_root / f"{stem}.ivault"
        encoded = encode_binary_document(
            _portable_sqlite_image(database_bytes),
            purpose="investment-database-pre-schema-migration",
        )
        _atomic_write_bytes(path, encoded)
        manifest = {
            "format": "gptbridge-investment-backup-v1",
            "backup_id": backup_id,
            "created_at": utc_text(),
            "schema_version": previous_schema_version,
            "target_schema_version": SCHEMA_VERSION,
            "encrypted": True,
            "files": [
                {
                    "role": "analytics_database",
                    "name": path.name,
                    "size": len(encoded),
                    "sha256": hashlib.sha256(encoded).hexdigest(),
                }
            ],
        }
        _atomic_write_bytes(
            self.backup_root / f"{stem}.manifest.json",
            (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )

    def _load_database(self) -> None:
        if not self.database_path.exists() or self.database_path.stat().st_size == 0:
            return
        try:
            database_bytes = self._read_database_bytes()
            result = self._database_connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise ValueError("investment analytics database integrity check failed")
            previous_schema_version = self._loaded_schema_version()
            if previous_schema_version > SCHEMA_VERSION:
                raise InvestmentAnalyticsUpgradeRequired(
                    "投資分析資料庫由較新的程式版本建立；"
                    "為避免舊版覆寫，請先升級投資管家。"
                )
            if previous_schema_version < SCHEMA_VERSION:
                self._preserve_pre_schema_migration(
                    database_bytes,
                    previous_schema_version=previous_schema_version,
                )
            self._durable_database_image = database_bytes
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            self._preserve_corrupt_database(exc)

    def _read_database_bytes(self) -> bytes:
        raw = self.database_path.read_bytes()
        if raw.startswith(b"SQLite format 3\x00"):
            source = sqlite3.connect(self.database_path)
            try:
                source.execute("PRAGMA busy_timeout = 10000")
                source.backup(self._database_connection)
            finally:
                source.close()
            return _portable_sqlite_image(
                self._database_connection.serialize()
            )
        database_bytes, envelope = decode_binary_document(raw)
        database_bytes = _portable_sqlite_image(database_bytes)
        self._database_key_id = str(envelope.get("key_id") or "")
        self._database_connection.deserialize(database_bytes)
        return database_bytes

    def _preserve_corrupt_database(self, exc: Exception) -> None:
        _validated_storage_path(
            self.recovery_root,
            label="AI investment analytics recovery root",
            boundary=self.runtime_root,
            expected_kind="directory",
        )
        self.recovery_root.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.recovery_root,
            label="AI investment analytics recovery root",
            boundary=self.runtime_root,
            require_exists=True,
            expected_kind="directory",
        )
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        preserved = self.recovery_root / f"investment_analytics.{stamp}.corrupt"
        try:
            _copy_verified(self.database_path, preserved)
        except OSError:
            preserved = self.database_path
        marker = {
            "status": "recovery_required",
            "detected_at": utc_text(),
            "source": str(self.database_path),
            "preserved_copy": str(preserved),
            "error_type": type(exc).__name__,
        }
        _atomic_write_bytes(
            self.recovery_root / "latest-database-recovery-required.json",
            (json.dumps(marker, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        raise RuntimeError(
            "投資分析資料庫無法解密或通過完整性檢查；原檔已保留，請從備份恢復。"
        ) from exc

    def _persist_database(self, *, rotate_key: bool = False) -> None:
        database_bytes = _portable_sqlite_image(self._database_connection.serialize())
        key_id = uuid.uuid4().hex if rotate_key or not self._database_key_id else self._database_key_id
        encoded = encode_binary_document(
            database_bytes,
            purpose="investment-analytics-database-v1",
            key_id=key_id,
        )
        _atomic_write_bytes(self.database_path, encoded)
        self._database_key_id = key_id
        self._durable_database_image = database_bytes


__all__ = ['AnalyticsStoreConnectionMixin']
