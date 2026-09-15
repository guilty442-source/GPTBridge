from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

from .analytics_common import (
    _RuntimeOwnerLock,
    _atomic_write_bytes,
    _copy_verified,
    _iter_migration_files,
    _runtime_root,
    _validated_storage_path,
    utc_text,
)


class AnalyticsStoreLifecycleMixin:
    """Storage roots, process ownership, and legacy data migration."""

    def __init__(self, tool_root: Path) -> None:
        self._init_storage_roots(tool_root)
        self._init_database_state()
        self._validate_storage_layout()
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

    def _init_storage_roots(self, tool_root: Path) -> None:
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

    def _init_database_state(self) -> None:
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

    def _validate_storage_layout(self) -> None:
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
        candidates = self._legacy_runtime_candidates(legacy_runtime)
        copied, conflicts = self._copy_legacy_runtime_candidates(candidates)
        if copied or conflicts:
            self._write_runtime_migration_manifest(copied, conflicts)

    def _legacy_runtime_candidates(
        self,
        legacy_runtime: Path,
    ) -> list[tuple[Path, Path]]:
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
        return candidates

    def _copy_legacy_runtime_candidates(
        self,
        candidates: list[tuple[Path, Path]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
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
        return copied, conflicts

    def _write_runtime_migration_manifest(
        self,
        copied: list[dict[str, Any]],
        conflicts: list[str],
    ) -> None:
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
                self._migrate_backup_generation(legacy_root, vault)
            try:
                legacy_root.rmdir()
            except OSError:
                pass

    def _migrate_backup_generation(self, legacy_root: Path, vault: Path) -> None:
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


__all__ = ['AnalyticsStoreLifecycleMixin']
