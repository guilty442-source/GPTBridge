from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from typing import Any

from .analytics_common import (
    InvestmentAnalyticsUpgradeRequired,
    SCHEMA_VERSION,
    _atomic_write_bytes,
    _copy_verified,
    _fsync_directory,
    _portable_sqlite_image,
    _validated_storage_path,
    _iter_migration_files,
    decode_binary_document,
    encode_binary_document,
    utc_now,
    utc_text,
)


class DatabaseMixin:
    """Database lifecycle, persistence, and legacy migration methods."""

    def _migrate_legacy_runtime(self) -> None:
        if self.runtime_root == self.legacy_runtime_root:
            return
        if (
            not self.legacy_runtime_root.exists()
            and not self.legacy_runtime_root.is_symlink()
        ):
            return
        legacy_runtime = _validated_storage_path(
            self.legacy_runtime_root,
            label="Legacy AI investment analytics runtime root",
            boundary=self.tool_root,
            require_exists=True,
            expected_kind="directory",
        )
        candidates: list[tuple[Any, Any]] = []
        legacy_database = legacy_runtime / self.database_path.name
        if legacy_database.exists() or legacy_database.is_symlink():
            candidates.append(
                (
                    _validated_storage_path(
                        legacy_database,
                        label="Legacy investment analytics database",
                        boundary=legacy_runtime,
                        require_exists=True,
                        expected_kind="file",
                    ),
                    self.database_path,
                )
            )
        for relative_root in ("state",):
            source_root = legacy_runtime / relative_root
            if not source_root.exists() and not source_root.is_symlink():
                continue
            source_root = _validated_storage_path(
                source_root,
                label=f"Legacy investment analytics {relative_root} root",
                boundary=legacy_runtime,
                require_exists=True,
                expected_kind="directory",
            )
            for source in _iter_migration_files(
                source_root,
                label=f"Legacy investment analytics {relative_root}",
            ):
                destination = _validated_storage_path(
                    self.runtime_root / source.relative_to(legacy_runtime),
                    label="Migrated investment analytics file",
                    boundary=self.runtime_root,
                    expected_kind="file",
                )
                candidates.append((source, destination))
        copied: list[dict[str, Any]] = []
        conflicts: list[str] = []
        for source, destination in candidates:
            relative = destination.relative_to(self.runtime_root)
            if destination.exists():
                if hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(source.read_bytes()).digest():
                    conflicts.append(str(relative))
                continue
            _copy_verified(source, destination)
            copied.append(
                {
                    "path": str(relative),
                    "size": source.stat().st_size,
                    "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                }
            )
        if copied or conflicts:
            manifest = {
                "migration": "investment-runtime-v1",
                "created_at": utc_text(),
                "source": str(self.legacy_runtime_root),
                "destination": str(self.runtime_root),
                "copied": copied,
                "conflicts": conflicts,
                "legacy_preserved": True,
            }
            _atomic_write_bytes(
                self.runtime_root / "runtime-migration-manifest.json",
                (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
            )

    def _migrate_legacy_backups(self) -> None:
        """Move published backup generations into project-cleaner's storage."""

        legacy_roots = {
            self.legacy_runtime_root / "investment_backups",
            self.runtime_root / "investment_backups",
        }
        for candidate in legacy_roots:
            if candidate.resolve() == self.backup_root.resolve():
                continue
            if not candidate.exists() and not candidate.is_symlink():
                continue
            legacy_root = _validated_storage_path(
                candidate,
                label="Legacy AI investment backup root",
                require_exists=True,
                expected_kind="directory",
            )
            generations = sorted(
                legacy_root.glob("*.ivault"),
                key=lambda item: item.stat().st_mtime_ns,
            )
            for vault in generations:
                related = (
                    vault,
                    vault.with_name(f"{vault.stem}.manifest.json"),
                    vault.with_name(f"{vault.stem}.statevault"),
                )
                for source in related:
                    if not source.exists():
                        continue
                    source_stat = source.stat()
                    destination = self.backup_root / source.name
                    if destination.exists():
                        source_digest = hashlib.sha256(source.read_bytes()).digest()
                        destination_digest = hashlib.sha256(destination.read_bytes()).digest()
                        if source_digest != destination_digest:
                            continue
                    else:
                        _copy_verified(source, destination)
                        os.utime(
                            destination,
                            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns),
                        )
                    source.unlink()
            try:
                legacy_root.rmdir()
            except OSError:
                pass

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
            raw = self.database_path.read_bytes()
            if raw.startswith(b"SQLite format 3\x00"):
                source = sqlite3.connect(self.database_path)
                try:
                    source.execute("PRAGMA busy_timeout = 10000")
                    source.backup(self._database_connection)
                finally:
                    source.close()
                database_bytes = _portable_sqlite_image(
                    self._database_connection.serialize()
                )
            else:
                database_bytes, envelope = decode_binary_document(raw)
                database_bytes = _portable_sqlite_image(database_bytes)
                self._database_key_id = str(envelope.get("key_id") or "")
                self._database_connection.deserialize(database_bytes)
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
