from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .analytics_common import (
    SCHEMA_VERSION,
    _atomic_write_bytes,
    _fsync_directory,
    _portable_sqlite_image,
    _validated_storage_path,
    decode_binary_document,
    decode_json_document,
    encode_binary_document,
    utc_now,
    utc_text,
)


class BackupMixin:
    """Backup, restore, and database security methods."""

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
        max_total: int = 1,
        max_automatic: int = 1,
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

        retained_limit = 0 if remove_all else 1
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
