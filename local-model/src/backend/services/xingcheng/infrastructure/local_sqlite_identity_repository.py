from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .local_sqlite_pool import LocalSqlitePoolManager, get_pool_manager


class LocalSqliteIdentityRepository:
    """Local sqlite3 source of truth for the Xingcheng role identity.

    Replaces the retired ``PostgresIdentityRepository`` (``role_data``/``role_history``/
    ``role_audit`` schemas).  Governs the plaintext personality plus its version
    history and audit trail on the local store.  At most one personality exists;
    every mutation records an immutable version and an audit event.  No PostgreSQL/
    psycopg, no external service (A44/E30).
    """

    SCHEMAS = ("role_data", "role_history", "role_audit")

    def __init__(self, tool_root: Path, pool_manager: LocalSqlitePoolManager | None = None):
        self.tool_root = Path(tool_root).resolve()
        self._manager = pool_manager or get_pool_manager(self.tool_root)
        self.database_path = (
            self.tool_root / "xingcheng" / "runtime" / "state" / "identity.sqlite3"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS role_data_personality (
                    resource_id TEXT NOT NULL PRIMARY KEY,
                    platform_id TEXT NOT NULL DEFAULT 'local-model-platform',
                    module_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    data_category TEXT NOT NULL DEFAULT 'role-setting',
                    resource_type TEXT NOT NULL DEFAULT 'personality',
                    resource_label TEXT NOT NULL,
                    classification TEXT NOT NULL DEFAULT 'private',
                    value TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS role_history_personality_version (
                    resource_id TEXT NOT NULL,
                    platform_id TEXT NOT NULL DEFAULT 'local-model-platform',
                    module_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    data_category TEXT NOT NULL DEFAULT 'role-setting',
                    resource_type TEXT NOT NULL DEFAULT 'personality-version',
                    resource_label TEXT NOT NULL,
                    classification TEXT NOT NULL DEFAULT 'private',
                    value TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (resource_id, version)
                );
                CREATE TABLE IF NOT EXISTS role_audit_event (
                    resource_id TEXT NOT NULL,
                    platform_id TEXT NOT NULL DEFAULT 'local-model-platform',
                    module_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    data_category TEXT NOT NULL DEFAULT 'role-setting-audit',
                    resource_type TEXT NOT NULL DEFAULT 'audit-event',
                    resource_label TEXT NOT NULL,
                    classification TEXT NOT NULL DEFAULT 'private',
                    value TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    content_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    event_id TEXT NOT NULL PRIMARY KEY
                );
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _require_identity_db(self) -> None:
        if not self.database_path.parent.exists():
            raise RuntimeError("GPTBRIDGE_XINGCHENG_IDENTITY_DB_UNAVAILABLE")

    def initialized(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'role_data_personality'"
            ).fetchone()
        return bool(row)

    def personality(self, *, module_id: str = "xingcheng") -> dict[str, Any] | None:
        self._require_identity_db()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT resource_id, module_id, owner_id, resource_label,
                       classification, value, version, content_hash,
                       created_at, updated_at
                FROM role_data_personality
                WHERE module_id = ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (module_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "resource_id": str(row["resource_id"]),
            "module_id": str(row["module_id"]),
            "owner_id": str(row["owner_id"]),
            "resource_label": str(row["resource_label"]),
            "classification": str(row["classification"]),
            "value": self._loads(row["value"]),
            "version": int(row["version"]),
            "content_hash": str(row["content_hash"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def personality_versions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self._require_identity_db()
        bounded = max(1, min(200, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resource_id, resource_label, value, version,
                       content_hash, created_at
                FROM role_history_personality_version
                ORDER BY version DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [
            {
                "resource_id": str(row["resource_id"]),
                "resource_label": str(row["resource_label"]),
                "value": self._loads(row["value"]),
                "version": int(row["version"]),
                "content_hash": str(row["content_hash"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def save_personality(
        self,
        value: dict[str, Any],
        *,
        module_id: str = "xingcheng",
        owner_id: str = "xingcheng",
        classification: str = "private",
        resource_label: str = "",
    ) -> dict[str, Any]:
        self._require_identity_db()
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        resource_id = f"role-personality:{content_hash[:24]}"
        label = str(resource_label or "星澄角色設定").strip()[:255]
        resource = self.personality(module_id=module_id)
        if resource is None:
            version = 1
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO role_data_personality (
                        resource_id, platform_id, module_id, owner_id,
                        data_category, resource_type, resource_label,
                        classification, value, version, content_hash
                    ) VALUES (?, 'local-model-platform', ?, ?,
                              'role-setting', 'personality', ?, ?, ?,
                              ?, ?)
                    """,
                    (
                        resource_id, module_id, owner_id, label,
                        classification, serialized, version, content_hash,
                    ),
                )
                self._append_version(
                    connection, resource_id, module_id, owner_id,
                    label, classification, serialized, version, content_hash,
                )
                self._append_audit(
                    connection, resource_id, module_id, owner_id, "create",
                    {"version": version},
                )
        else:
            previous = int(resource["version"])
            version = previous + 1
            with self._connect() as connection:
                connection.execute(
                    """
                    UPDATE role_data_personality
                    SET value = ?, version = ?, content_hash = ?,
                        resource_label = ?, classification = ?
                    WHERE resource_id = ?
                    """,
                    (
                        serialized, version, content_hash, label,
                        classification, resource["resource_id"],
                    ),
                )
                self._append_version(
                    connection, resource["resource_id"], module_id, owner_id,
                    label, classification, serialized, version, content_hash,
                )
                self._append_audit(
                    connection, resource["resource_id"], module_id, owner_id,
                    "update", {"previous_version": previous, "version": version},
                )
            resource_id = str(resource["resource_id"])
        return {
            "ok": True,
            "resource_id": resource_id,
            "resource_label": label,
            "version": version,
            "content_hash": content_hash,
            "classification": classification,
            "module_id": module_id,
            "owner_id": owner_id,
        }

    def audit_events(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self._require_identity_db()
        bounded = max(1, min(200, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resource_id, owner_id, resource_label, value,
                       version, created_at
                FROM role_audit_event
                ORDER BY created_at DESC LIMIT ?
                """,
                (bounded,),
            ).fetchall()
        return [
            {
                "resource_id": str(row["resource_id"]),
                "owner_id": str(row["owner_id"]),
                "resource_label": str(row["resource_label"]),
                "value": self._loads(row["value"]),
                "version": int(row["version"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def _append_version(
        self,
        connection: sqlite3.Connection,
        resource_id: str,
        module_id: str,
        owner_id: str,
        label: str,
        classification: str,
        serialized: str,
        version: int,
        content_hash: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO role_history_personality_version (
                resource_id, platform_id, module_id, owner_id,
                data_category, resource_type, resource_label, classification,
                value, version, content_hash
            ) VALUES (?, 'local-model-platform', ?, ?,
                      'role-setting', 'personality-version', ?, ?,
                      ?, ?, ?)
            """,
            (
                resource_id, module_id, owner_id, label, classification,
                serialized, version, content_hash,
            ),
        )

    def _append_audit(
        self,
        connection: sqlite3.Connection,
        resource_id: str,
        module_id: str,
        owner_id: str,
        action: str,
        detail: dict[str, Any],
    ) -> None:
        payload = {"action": action, **detail}
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        event_id = f"role-audit:{uuid.uuid4().hex[:24]}"
        connection.execute(
            """
            INSERT INTO role_audit_event (
                event_id, resource_id, platform_id, module_id, owner_id,
                data_category, resource_type, resource_label,
                classification, value, version, content_hash
            ) VALUES (?, ?, 'local-model-platform', ?, ?,
                      'role-setting-audit', 'audit-event', ?,
                      'private', ?, 1, ?)
            """,
            (
                event_id, resource_id, module_id, owner_id,
                f"role-{action}:{resource_id[-16:]}", serialized,
                hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            ),
        )

    @staticmethod
    def _loads(value: Any) -> Any:
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value


__all__ = ["LocalSqliteIdentityRepository"]