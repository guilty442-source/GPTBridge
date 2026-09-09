from __future__ import annotations

import json
import os
import uuid
from typing import Any

from .analytics_common import (
    _decoded_json,
    _json,
    privacy_status,
    protect_text,
    unprotect_text,
    utc_text,
)


class SettingsAuditMixin:
    """Settings, audit logging, and database security methods."""

    def set_setting(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO settings(key, value_json, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_at=excluded.updated_at",
                (key, _json(value), utc_text()),
            )

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            row = connection.execute("SELECT value_json FROM settings WHERE key = ?", (key,)).fetchone()
        return _decoded_json(row["value_json"], default) if row else default

    def audit(self, action: str, details: dict[str, Any], *, severity: str = "info") -> None:
        audit_id = uuid.uuid4().hex
        occurred_at = utc_text()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(audit_id, occurred_at, action, severity, details_encrypted) VALUES(?, ?, ?, ?, ?)",
                (audit_id, occurred_at, action, severity, protect_text(_json(details))),
            )
        self._append_managed_audit_record(
            audit_id=audit_id,
            occurred_at=occurred_at,
            action=action,
            severity=severity,
            details=details,
        )

    def _append_managed_audit_record(
        self,
        *,
        audit_id: str,
        occurred_at: str,
        action: str,
        severity: str,
        details: dict[str, Any],
    ) -> None:
        record = {
            "audit_id": audit_id,
            "occurred_at": occurred_at,
            "tool_id": "ai-assistant",
            "action": action,
            "severity": severity,
            "details_encrypted": protect_text(_json(details)),
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            target.flush()
            os.fsync(target.fileno())

    def list_audit_log(self, limit: int = 200) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_log ORDER BY occurred_at DESC LIMIT ?",
                (max(1, min(2000, int(limit))),),
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["details"] = _decoded_json(
                unprotect_text(str(item.pop("details_encrypted", "") or "")), {}
            )
            output.append(item)
        return output

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
