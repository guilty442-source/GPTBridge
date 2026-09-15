from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .analytics_common import (
    _atomic_write_bytes,
    _fsync_directory,
    _portable_sqlite_image,
    _validated_storage_path,
    decode_binary_document,
    decode_json_document,
    privacy_status,
    utc_now,
    utc_text,
)


class AnalyticsStoreRestoreMixin:
    """Backup restore, key rotation, and security status methods."""

    def restore_database(self, backup_name: str) -> dict[str, Any]:
        candidate = (self.backup_root / Path(str(backup_name)).name).resolve()
        if candidate.parent != self.backup_root.resolve() or not candidate.exists():
            raise ValueError("backup does not exist")
        candidate_payload = candidate.read_bytes()
        state_payload = self._verified_backup_state(candidate)
        database_bytes = self._validated_backup_database(candidate_payload)
        safety = self.backup_database("before-restore")
        with self._database_lock:
            originals = self._capture_restore_originals()
            try:
                self._apply_restore(database_bytes, state_payload, originals)
            except Exception as restore_error:
                self._rollback_restore(candidate, safety, restore_error, originals)
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

    def _verified_backup_state(self, candidate: Path) -> bytes | None:
        manifest_path = candidate.with_name(
            f"{candidate.stem}.manifest.json"
        )
        state_payload: bytes | None = None
        if not manifest_path.exists():
            return None
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
            state_payload = self._verify_backup_entry(item, state_payload) or state_payload
        return state_payload

    def _verify_backup_entry(
        self,
        item: Any,
        state_payload: bytes | None,
    ) -> bytes | None:
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
            return payload
        return None

    def _validated_backup_database(self, candidate_payload: bytes) -> bytes:
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
        return database_bytes

    def _capture_restore_originals(self) -> dict[str, Any]:
        state_path = (
            self.runtime_root / "state" / "investment_watch_state.json"
        )
        return {
            "database_payload": (
                self.database_path.read_bytes()
                if self.database_path.exists()
                else None
            ),
            "database_image": _portable_sqlite_image(
                self._database_connection.serialize()
            ),
            "durable_image": self._durable_database_image,
            "key_id": self._database_key_id,
            "state_path": state_path,
            "state_payload": (
                state_path.read_bytes() if state_path.exists() else None
            ),
        }

    def _apply_restore(
        self,
        database_bytes: bytes,
        state_payload: bytes | None,
        originals: dict[str, Any],
    ) -> None:
        self._database_connection.deserialize(database_bytes)
        self._database_connection.row_factory = sqlite3.Row
        self._database_connection.execute("PRAGMA foreign_keys = ON")
        self._database_connection.execute("PRAGMA busy_timeout = 10000")
        self._persist_database(rotate_key=True)
        if state_payload is not None:
            _atomic_write_bytes(originals["state_path"], state_payload)

    def _rollback_restore(
        self,
        candidate: Path,
        safety: dict[str, Any],
        restore_error: Exception,
        originals: dict[str, Any],
    ) -> None:
        rollback_errors: list[str] = []
        rollback_errors += self._rollback_database_image(originals)
        rollback_errors += self._rollback_state_payload(originals)
        if rollback_errors:
            self._record_restore_rollback(
                candidate,
                safety,
                restore_error,
                rollback_errors,
            )

    def _rollback_database_image(self, originals: dict[str, Any]) -> list[str]:
        try:
            self._database_connection.deserialize(
                originals["database_image"]
            )
            self._database_connection.row_factory = sqlite3.Row
            self._database_connection.execute(
                "PRAGMA foreign_keys = ON"
            )
            self._database_connection.execute(
                "PRAGMA busy_timeout = 10000"
            )
            self._durable_database_image = originals["durable_image"]
            self._database_key_id = originals["key_id"]
            if originals["database_payload"] is not None:
                _atomic_write_bytes(
                    self.database_path,
                    originals["database_payload"],
                )
        except Exception as rollback_error:
            return [f"database:{type(rollback_error).__name__}"]
        return []

    def _rollback_state_payload(self, originals: dict[str, Any]) -> list[str]:
        state_path = originals["state_path"]
        try:
            if originals["state_payload"] is not None:
                _atomic_write_bytes(
                    state_path,
                    originals["state_payload"],
                )
            elif state_path.exists():
                self._preserve_failed_state(state_path)
        except Exception as rollback_error:
            return [f"state:{type(rollback_error).__name__}"]
        return []

    def _preserve_failed_state(self, state_path: Path) -> None:
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

    def _record_restore_rollback(
        self,
        candidate: Path,
        safety: dict[str, Any],
        restore_error: Exception,
        rollback_errors: list[str],
    ) -> None:
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


__all__ = ['AnalyticsStoreRestoreMixin']
