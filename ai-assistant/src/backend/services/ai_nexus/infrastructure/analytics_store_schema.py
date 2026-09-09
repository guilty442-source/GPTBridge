from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import urllib.error
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

from .privacy import (
    decode_binary_document,
    decode_json_document,
    encode_binary_document,
    privacy_status,
    protect_text,
    unprotect_text,
)
from .watch_repository import (
    _iter_migration_files,
    _validated_storage_path,
)
from ..domain.contract import INVESTMENT_ANALYTICS_SCHEMA_VERSION


SCHEMA_VERSION = INVESTMENT_ANALYTICS_SCHEMA_VERSION
DEFAULT_ALERT_COOLDOWN_MINUTES = 240
OPENING_BALANCE_PREFIX = "opening-balance:"
RECONCILIATION_PREFIX = "ledger-reconciliation:"
DEFAULT_BACKUP_RETENTION = 1
DEFAULT_AUTOMATIC_BACKUP_RETENTION = 1


class InvestmentAnalyticsUpgradeRequired(RuntimeError):
    """Raised when a database requires a newer investment manager."""


def _portable_sqlite_image(data: bytes) -> bytes:
    """Make serialized WAL databases self-contained for memory deserialization."""
    if len(data) >= 20 and data.startswith(b"SQLite format 3\x00") and data[18:20] == b"\x02\x02":
        normalized = bytearray(data)
        normalized[18] = 1
        normalized[19] = 1
        return bytes(normalized)
    return data



from .analytics_helpers import *
from .analytics_finance import *
from .analytics_io import *
class InvestmentAnalyticsUpgradeRequired(RuntimeError):
    """Raised when a database requires a newer investment manager."""




class InvestmentAnalyticsStoreSchema:

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = _validated_storage_path(
            Path(tool_root),
            label="AI assistant tool root",
            require_exists=True,
            expected_kind="directory",
        )
        self.legacy_runtime_root = _validated_storage_path(
            self.tool_root / "runtime",
            label="Legacy AI investment analytics runtime root",
            boundary=self.tool_root,
            expected_kind="directory",
        )
        self.runtime_root = _runtime_root(self.tool_root)
        self.database_path = self.runtime_root / "investment_analytics_v2.sqlite3"
        managed_storage = str(os.environ.get("GPTBRIDGE_MANAGED_STORAGE_ROOT") or "").strip()
        self._uses_external_managed_storage = bool(managed_storage)
        self.managed_storage_root = (
            Path(managed_storage).resolve()
            if managed_storage
            else self.runtime_root
        )
        self.backup_root = (
            self.managed_storage_root / "backups"
            if managed_storage
            else self.runtime_root / "investment_backups"
        )
        self.audit_root = (
            self.managed_storage_root / "audit" / "ai-assistant"
            if managed_storage
            else self.runtime_root / "audit"
        )
        self.audit_path = self.audit_root / "investment.jsonl"
        self.recovery_root = self.runtime_root / "recovery"
        self._database_lock = threading.RLock()
        self._database_key_id = ""
        self._batch_depth = 0
        self._pending_persist = False
        self._closed = False
        self._database_connection = sqlite3.connect(":memory:", check_same_thread=False)
        self._database_connection.row_factory = sqlite3.Row
        self._database_connection.execute("PRAGMA foreign_keys = ON")
        self._database_connection.execute("PRAGMA busy_timeout = 10000")
        self._durable_database_image: bytes | None = None
        _validated_storage_path(
            self.runtime_root,
            label="AI investment analytics runtime root",
            boundary=self.runtime_root,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.backup_root,
            label="AI investment analytics backup root",
            boundary=self.managed_storage_root,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.audit_root,
            label="AI investment analytics audit root",
            boundary=self.managed_storage_root,
            expected_kind="directory",
        )
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.runtime_root,
            label="AI investment analytics runtime root",
            boundary=self.runtime_root,
            require_exists=True,
            expected_kind="directory",
        )
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.audit_root.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.backup_root,
            label="AI investment analytics backup root",
            boundary=self.managed_storage_root,
            require_exists=True,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.database_path,
            label="AI investment analytics database",
            boundary=self.runtime_root,
            expected_kind="file",
        )
        self._owner_lock = _RuntimeOwnerLock(
            self.runtime_root / ".investment-analytics-owner.lock",
            "AI investment analytics",
        )
        try:
            self._owner_lock.acquire()
            self._migrate_legacy_runtime()
            self._migrate_legacy_backups()
            self.prune_backups()
            self._load_database()
            self.initialize()
        except Exception:
            self._database_connection.close()
            self._closed = True
            self._owner_lock.release()
            raise


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
        candidates: list[tuple[Path, Path]] = []
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
                raise
            else:
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


    def _preserve_incomplete_backup(
        self,
        paths: Sequence[Path],
        *,
        backup_id: str,
        error: Exception,
    ) -> None:
        recovery_dir = self.recovery_root / "incomplete-backups" / backup_id
        _validated_storage_path(
            recovery_dir,
            label="Incomplete investment backup recovery directory",
            boundary=self.runtime_root,
            expected_kind="directory",
        )
        recovery_dir.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            recovery_dir,
            label="Incomplete investment backup recovery directory",
            boundary=self.runtime_root,
            require_exists=True,
            expected_kind="directory",
        )
        preserved: list[str] = []
        for path in paths:
            if not path.exists():
                continue
            destination = recovery_dir / path.name
            os.replace(path, destination)
            preserved.append(str(destination))
        marker = {
            "status": "incomplete_backup_preserved",
            "detected_at": utc_text(),
            "backup_id": backup_id,
            "error_type": type(error).__name__,
            "preserved": preserved,
        }
        _atomic_write_bytes(
            recovery_dir / "recovery.json",
            (json.dumps(marker, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )
        _fsync_directory(recovery_dir)


    def backup_database(self, label: str = "manual") -> dict[str, Any]:
        del label
        with self._database_lock:
            self.prune_backups(remove_all=True)
            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
            backup_id = uuid.uuid4().hex
            stem = f"{stamp}-{backup_id[:8]}"
            path = self.backup_root / f"{stem}.ivault"
            manifest_path = self.backup_root / f"{stem}.manifest.json"
            state_path = self.backup_root / f"{stem}.statevault"
            created_paths: list[Path] = []
            try:
                encoded_database = encode_binary_document(
                    _portable_sqlite_image(
                        self._database_connection.serialize()
                    ),
                    purpose="investment-analytics-backup-v1",
                )
                _atomic_write_bytes(path, encoded_database)
                created_paths.append(path)
                files = [
                    {
                        "role": "analytics_database",
                        "name": path.name,
                        "size": len(encoded_database),
                        "sha256": hashlib.sha256(
                            encoded_database
                        ).hexdigest(),
                    }
                ]
                state_source = (
                    self.runtime_root
                    / "state"
                    / "investment_watch_state.json"
                )
                if (
                    state_source.is_file()
                    and state_source.stat().st_size > 0
                ):
                    state_payload = state_source.read_bytes()
                    # Validate protected state before publishing its backup.
                    decode_json_document(state_payload.decode("utf-8"))
                    _atomic_write_bytes(state_path, state_payload)
                    created_paths.append(state_path)
                    files.append(
                        {
                            "role": "portfolio_state",
                            "name": state_path.name,
                            "size": len(state_payload),
                            "sha256": hashlib.sha256(
                                state_payload
                            ).hexdigest(),
                        }
                    )
                created_at = utc_text()
                manifest = {
                    "format": "gptbridge-investment-backup-v1",
                    "backup_id": backup_id,
                    "created_at": created_at,
                    "schema_version": SCHEMA_VERSION,
                    "encrypted": True,
                    "files": files,
                }
                _atomic_write_bytes(
                    manifest_path,
                    (
                        json.dumps(
                            manifest,
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n"
                    ).encode("utf-8"),
                )
                created_paths.append(manifest_path)
            except Exception as backup_error:
                self._preserve_incomplete_backup(
                    created_paths,
                    backup_id=backup_id,
                    error=backup_error,
                )
                raise
        self.audit(
            "database_backup",
            {
                "backup_id": backup_id,
                "path": str(path),
                "manifest_path": str(manifest_path),
                "state_included": any(item["role"] == "portfolio_state" for item in files),
            },
        )
        self.prune_backups()
        return {
            "backup_id": backup_id,
            "path": str(path),
            "manifest_path": str(manifest_path),
            "created_at": created_at,
            "encrypted": True,
            "state_included": any(item["role"] == "portfolio_state" for item in files),
            "files": files,
        }


    def list_backups(self) -> list[dict[str, Any]]:
        output = []
        for path in sorted(self.backup_root.glob("*.ivault"), reverse=True)[:100]:
            manifest_path = path.with_name(
                f"{path.stem}.manifest.json"
            )
            manifest: dict[str, Any] = {}
            integrity = None
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    declared = next(
                        (
                            item
                            for item in manifest.get("files", [])
                            if isinstance(item, dict) and item.get("role") == "analytics_database"
                        ),
                        {},
                    )
                    integrity = (
                        int(declared.get("size") or -1) == path.stat().st_size
                        and str(declared.get("sha256") or "") == hashlib.sha256(path.read_bytes()).hexdigest()
                    )
                except (OSError, ValueError, json.JSONDecodeError):
                    integrity = False
            output.append(
                {
                    "name": path.name,
                    "path": str(path),
                    "size": path.stat().st_size,
                    "modified_at": datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).isoformat(),
                    "backup_id": str(manifest.get("backup_id") or ""),
                    "manifest_path": str(manifest_path) if manifest_path.exists() else "",
                    "integrity_verified": integrity,
                    "state_included": any(
                        isinstance(item, dict) and item.get("role") == "portfolio_state"
                        for item in manifest.get("files", [])
                    ),
                }
            )
        return output


    def prune_backups(
        self,
        *,
        max_total: int = DEFAULT_BACKUP_RETENTION,
        max_automatic: int = DEFAULT_AUTOMATIC_BACKUP_RETENTION,
        remove_all: bool = False,
    ) -> dict[str, int]:
        # Retention is global across every published backup type. Arguments
        # remain for API compatibility but cannot change the one-copy limit.
        del max_total, max_automatic
        managed_storage = str(
            os.environ.get("GPTBRIDGE_MANAGED_STORAGE_ROOT") or ""
        ).strip()
        retention_root = (
            self.managed_storage_root / "backups"
            if managed_storage
            else self.backup_root
        )
        retention_root = _validated_storage_path(
            retention_root,
            label="Global managed backup root",
            boundary=self.managed_storage_root,
            require_exists=True,
            expected_kind="directory",
        )
        paths = sorted(
            (
                path
                for pattern in ("*.ivault", "*.zip")
                for path in retention_root.rglob(pattern)
                if path.is_file()
            ),
            key=lambda item: item.stat().st_mtime_ns,
            reverse=True,
        )

        retained_limit = 0 if remove_all else DEFAULT_BACKUP_RETENTION
        to_delete = paths[retained_limit:]

        removed = 0
        deleted_bytes = 0
        for path in to_delete:
            path = _validated_storage_path(
                path,
                label="Published backup generation",
                boundary=retention_root,
                require_exists=True,
                expected_kind="file",
            )
            sidecars = () if path.suffix.lower() == ".zip" else (
                path.with_name(f"{path.stem}.manifest.json"),
                path.with_name(f"{path.stem}.statevault"),
            )
            try:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                path.unlink()
                removed += 1
                deleted_bytes += size
            except OSError:
                # If a file cannot be unlinked, skip it and continue with others
                continue
            for sidecar in sidecars:
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass

        retained = sum(
            1
            for pattern in ("*.ivault", "*.zip")
            for path in retention_root.rglob(pattern)
            if path.is_file()
        )

        return {
            "retained": retained,
            "removed": removed,
            "deleted_bytes": deleted_bytes,
            "over_total_limit": max(0, retained - retained_limit),
            "over_automatic_limit": 0,
        }


    def restore_database(self, backup_name: str) -> dict[str, Any]:
        candidate = (self.backup_root / Path(str(backup_name)).name).resolve()
        if candidate.parent != self.backup_root.resolve() or not candidate.exists():
            raise ValueError("backup does not exist")
        candidate_payload = candidate.read_bytes()
        manifest_path = candidate.with_name(
            f"{candidate.stem}.manifest.json"
        )
        state_payload: bytes | None = None
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("format") != "gptbridge-investment-backup-v1":
                raise ValueError("unsupported backup manifest")
            database_entries = [
                item
                for item in manifest.get("files", [])
                if isinstance(item, dict)
                and item.get("role") == "analytics_database"
            ]
            state_entries = [
                item
                for item in manifest.get("files", [])
                if isinstance(item, dict)
                and item.get("role") == "portfolio_state"
            ]
            if (
                len(database_entries) != 1
                or Path(str(database_entries[0].get("name") or "")).name
                != candidate.name
                or len(state_entries) > 1
            ):
                raise ValueError("backup manifest is incomplete or ambiguous")
            for item in manifest.get("files", []):
                if not isinstance(item, dict):
                    raise ValueError("backup manifest is invalid")
                related = (self.backup_root / Path(str(item.get("name") or "")).name).resolve()
                if related.parent != self.backup_root.resolve() or not related.exists():
                    raise ValueError("backup is incomplete")
                payload = related.read_bytes()
                if len(payload) != int(item.get("size") or -1):
                    raise ValueError("backup size verification failed")
                if hashlib.sha256(payload).hexdigest() != str(item.get("sha256") or ""):
                    raise ValueError("backup hash verification failed")
                if item.get("role") == "portfolio_state":
                    decode_json_document(payload.decode("utf-8"))
                    state_payload = payload
        database_bytes, _envelope = decode_binary_document(candidate_payload)
        database_bytes = _portable_sqlite_image(database_bytes)
        validation = sqlite3.connect(":memory:")
        try:
            validation.deserialize(database_bytes)
            result = validation.execute("PRAGMA integrity_check").fetchone()
            if not result or str(result[0]).lower() != "ok":
                raise ValueError("backup integrity check failed")
        finally:
            validation.close()
        safety = self.backup_database("before-restore")
        with self._database_lock:
            original_database_payload = (
                self.database_path.read_bytes()
                if self.database_path.exists()
                else None
            )
            original_database_image = _portable_sqlite_image(
                self._database_connection.serialize()
            )
            original_durable_image = self._durable_database_image
            original_key_id = self._database_key_id
            state_path = (
                self.runtime_root / "state" / "investment_watch_state.json"
            )
            original_state_payload = (
                state_path.read_bytes() if state_path.exists() else None
            )
            try:
                self._database_connection.deserialize(database_bytes)
                self._database_connection.row_factory = sqlite3.Row
                self._database_connection.execute("PRAGMA foreign_keys = ON")
                self._database_connection.execute("PRAGMA busy_timeout = 10000")
                self._persist_database(rotate_key=True)
                if state_payload is not None:
                    _atomic_write_bytes(state_path, state_payload)
            except Exception as restore_error:
                rollback_errors: list[str] = []
                try:
                    self._database_connection.deserialize(
                        original_database_image
                    )
                    self._database_connection.row_factory = sqlite3.Row
                    self._database_connection.execute(
                        "PRAGMA foreign_keys = ON"
                    )
                    self._database_connection.execute(
                        "PRAGMA busy_timeout = 10000"
                    )
                    self._durable_database_image = original_durable_image
                    self._database_key_id = original_key_id
                    if original_database_payload is not None:
                        _atomic_write_bytes(
                            self.database_path,
                            original_database_payload,
                        )
                except Exception as rollback_error:
                    rollback_errors.append(
                        f"database:{type(rollback_error).__name__}"
                    )
                try:
                    if original_state_payload is not None:
                        _atomic_write_bytes(
                            state_path,
                            original_state_payload,
                        )
                    elif state_path.exists():
                        failed_state_root = (
                            self.recovery_root
                            / "failed-restore-state"
                        )
                        _validated_storage_path(
                            failed_state_root,
                            label="Failed restore state recovery root",
                            boundary=self.runtime_root,
                            expected_kind="directory",
                        )
                        failed_state_root.mkdir(
                            parents=True,
                            exist_ok=True,
                        )
                        _validated_storage_path(
                            failed_state_root,
                            label="Failed restore state recovery root",
                            boundary=self.runtime_root,
                            require_exists=True,
                            expected_kind="directory",
                        )
                        preserved_state = (
                            failed_state_root
                            / (
                                f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-"
                                f"{uuid.uuid4().hex}.statevault"
                            )
                        )
                        os.replace(state_path, preserved_state)
                        _fsync_directory(failed_state_root)
                        _fsync_directory(state_path.parent)
                except Exception as rollback_error:
                    rollback_errors.append(
                        f"state:{type(rollback_error).__name__}"
                    )
                if rollback_errors:
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
                    marker = {
                        "status": "restore_rollback_requires_review",
                        "detected_at": utc_text(),
                        "backup": candidate.name,
                        "safety_backup": safety["path"],
                        "restore_error_type": type(restore_error).__name__,
                        "rollback_errors": rollback_errors,
                    }
                    _atomic_write_bytes(
                        self.recovery_root
                        / "latest-restore-rollback-required.json",
                        (
                            json.dumps(
                                marker,
                                ensure_ascii=False,
                                indent=2,
                            )
                            + "\n"
                        ).encode("utf-8"),
                    )
                raise
        self.audit(
            "database_restore",
            {
                "backup": candidate.name,
                "safety_backup": safety["path"],
                "state_restored": state_payload is not None,
            },
            severity="warning",
        )
        return {
            "restored": True,
            "backup": candidate.name,
            "safety_backup": safety,
            "state_restored": state_payload is not None,
        }


    def rotate_database_protection(self) -> dict[str, Any]:
        previous = self._database_key_id
        safety = self.backup_database("before-key-rotation")
        with self._database_lock:
            self._persist_database(rotate_key=True)
        self.audit(
            "database_key_rotation",
            {"previous_key_id": previous, "new_key_id": self._database_key_id},
        )
        return {
            "rotated": True,
            "previous_key_id": previous,
            "key_id": self._database_key_id,
            "safety_backup": safety,
        }


    def database_security_status(self) -> dict[str, Any]:
        raw_prefix = self.database_path.read_bytes()[:16] if self.database_path.exists() else b""
        return {
            "encrypted_at_rest": bool(raw_prefix and not raw_prefix.startswith(b"SQLite format 3")),
            "protection": privacy_status().get("database_encryption"),
            "key_id": self._database_key_id,
            "backup_count": len(self.list_backups()),
            "database_path": str(self.database_path),
        }


    def __del__(self) -> None:
        """Release process resources without performing shutdown-time I/O.

        Every mutation is durably persisted by its operation. Explicit
        ``close()`` performs the final encrypted snapshot; garbage collection
        must not invoke DPAPI or filesystem writes while Python is finalizing.
        """

        if getattr(self, "_closed", True):
            return
        try:
            connection = getattr(self, "_database_connection", None)
            if connection is not None:
                connection.close()
        except BaseException:
            pass
        finally:
            self._closed = True
            try:
                owner_lock = getattr(self, "_owner_lock", None)
                if owner_lock is not None:
                    owner_lock.release()
            except BaseException:
                pass


    def _migrate_schema(self, connection: sqlite3.Connection) -> None:
        transaction_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(transactions)").fetchall()
        }
        transaction_additions = {
            "deleted_at": "TEXT NOT NULL DEFAULT ''",
            "delete_reason_encrypted": "TEXT NOT NULL DEFAULT ''",
            "delete_audit_id": "TEXT NOT NULL DEFAULT ''",
        }
        for column, declaration in transaction_additions.items():
            if column not in transaction_columns:
                connection.execute(
                    f"ALTER TABLE transactions ADD COLUMN {column} {declaration}"
                )

        decision_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(decisions)").fetchall()
        }
        additions = {
            "prediction_direction": "TEXT NOT NULL DEFAULT 'abstain'",
            "horizon_days": "INTEGER NOT NULL DEFAULT 30",
            "return_threshold_percent": "REAL NOT NULL DEFAULT 0",
            "eligible_for_calibration": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in additions.items():
            if column not in decision_columns:
                connection.execute(f"ALTER TABLE decisions ADD COLUMN {column} {declaration}")
        marker = connection.execute(
            "SELECT value FROM metadata WHERE key='decision_confidence_normalized_v1'"
        ).fetchone()
        if marker is None:
            # v3 accepted both scales, then divided all inputs by 100. Values at or
            # below 0.01 are therefore recognizable legacy 0..1 inputs.
            connection.execute(
                """
                UPDATE decisions
                SET confidence = CASE
                    WHEN confidence IS NULL THEN NULL
                    WHEN confidence < 0 THEN 0
                    WHEN confidence > 100 THEN 1
                    WHEN confidence > 1 THEN confidence / 100.0
                    WHEN confidence > 0 AND confidence <= 0.01 THEN confidence * 100.0
                    ELSE confidence
                END
                """
            )
            connection.execute(
                "INSERT INTO metadata(key, value, updated_at) VALUES(?, ?, ?)",
                ("decision_confidence_normalized_v1", "complete", utc_text()),
            )



__all__ = ['InvestmentAnalyticsStoreSchema']
