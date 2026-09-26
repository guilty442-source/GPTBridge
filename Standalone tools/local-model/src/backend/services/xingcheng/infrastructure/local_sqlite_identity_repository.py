from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from shared_layer.local.pg_adapter import PgConnection
from shared_layer.local.pg_adapter import connect as pg_connect


class LocalSqliteIdentityRepository:
    """PostgreSQL source of truth for the Xingcheng role identity.

    Absorbs the retired sqlite ``identity.sqlite3`` store (A610/A621) into the
    module-private ``gptbridge_xingcheng`` schema.  Governs the plaintext
    personality plus its version history and audit trail.  At most one
    personality exists; every mutation records an immutable version and an
    audit event.
    """

    SCHEMA = "gptbridge_xingcheng"
    SCHEMAS = ("role_data", "role_history", "role_audit")

    def __init__(self, tool_root: Path, pool_manager: Any = None):
        self.tool_root = Path(tool_root).resolve()
        self._manager = pool_manager
        self.database_path = Path(f"postgresql:{self.SCHEMA}")
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
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
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
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
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
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                    event_id TEXT NOT NULL PRIMARY KEY
                );
                """
            )

    @contextmanager
    def _connect(self) -> Iterator[PgConnection]:
        connection = pg_connect(self.SCHEMA, autocommit=False)
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _require_identity_db(self) -> None:
        return None  # schema availability is enforced fail-closed by pg_connect

    def initialized(self) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_type = 'BASE TABLE' AND table_name = 'role_data_personality'"
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
        resource_id = f"role-personality-{content_hash[:24]}"
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
        connection: PgConnection,
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
        connection: PgConnection,
        resource_id: str,
        module_id: str,
        owner_id: str,
        action: str,
        detail: dict[str, Any],
    ) -> None:
        payload = {"action": action, **detail}
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        event_id = f"role-audit-{uuid.uuid4().hex[:24]}"
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