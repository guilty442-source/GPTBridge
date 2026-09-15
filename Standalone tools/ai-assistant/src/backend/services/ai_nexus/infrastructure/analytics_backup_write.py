from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Sequence

from .analytics_common import (
    SCHEMA_VERSION,
    _atomic_write_bytes,
    _fsync_directory,
    _portable_sqlite_image,
    _validated_storage_path,
    decode_json_document,
    encode_binary_document,
    utc_now,
    utc_text,
)


class BackupWriteMixin:
    """Backup creation and incomplete-backup preservation."""

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
                files, created_at = self._write_backup_payloads(
                    backup_id,
                    path,
                    state_path,
                    manifest_path,
                    created_paths,
                )
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

    def _write_backup_payloads(
        self,
        backup_id: str,
        path: Path,
        state_path: Path,
        manifest_path: Path,
        created_paths: list[Path],
    ) -> tuple[list[dict[str, Any]], str]:
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
            self._write_state_backup(
                state_source,
                state_path,
                files,
                created_paths,
            )
        created_at = utc_text()
        self._write_backup_manifest(
            manifest_path,
            backup_id,
            created_at,
            files,
        )
        created_paths.append(manifest_path)
        return files, created_at

    @staticmethod
    def _write_backup_manifest(
        manifest_path: Path,
        backup_id: str,
        created_at: str,
        files: list[dict[str, Any]],
    ) -> None:
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

    @staticmethod
    def _write_state_backup(
        state_source: Path,
        state_path: Path,
        files: list[dict[str, Any]],
        created_paths: list[Path],
    ) -> None:
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
