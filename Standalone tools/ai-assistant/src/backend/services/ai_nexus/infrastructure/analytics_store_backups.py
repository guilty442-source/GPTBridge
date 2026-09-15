from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .analytics_common import (
    DEFAULT_AUTOMATIC_BACKUP_RETENTION,
    DEFAULT_BACKUP_RETENTION,
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


class AnalyticsStoreBackupMixin:
    """Backup creation, retention, and listing methods."""

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
                files, created_at = self._write_backup_files(
                    path,
                    manifest_path,
                    state_path,
                    backup_id=backup_id,
                    created_paths=created_paths,
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

    def _write_backup_files(
        self,
        path: Path,
        manifest_path: Path,
        state_path: Path,
        *,
        backup_id: str,
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
        self._backup_portfolio_state(state_path, files, created_paths)
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
        return files, created_at

    def _backup_portfolio_state(
        self,
        state_path: Path,
        files: list[dict[str, Any]],
        created_paths: list[Path],
    ) -> None:
        state_source = (
            self.runtime_root
            / "state"
            / "investment_watch_state.json"
        )
        if not (
            state_source.is_file()
            and state_source.stat().st_size > 0
        ):
            return
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
        retention_root = self._backup_retention_root()
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
        removed, deleted_bytes = self._delete_backup_generations(
            paths[retained_limit:],
            retention_root,
        )
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

    def _backup_retention_root(self) -> Path:
        managed_storage = str(
            os.environ.get("GPTBRIDGE_MANAGED_STORAGE_ROOT") or ""
        ).strip()
        retention_root = (
            self.managed_storage_root / "backups"
            if managed_storage
            else self.backup_root
        )
        return _validated_storage_path(
            retention_root,
            label="Global managed backup root",
            boundary=self.managed_storage_root,
            require_exists=True,
            expected_kind="directory",
        )

    def _delete_backup_generations(
        self,
        to_delete: Sequence[Path],
        retention_root: Path,
    ) -> tuple[int, int]:
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
        return removed, deleted_bytes


__all__ = ['AnalyticsStoreBackupMixin']
