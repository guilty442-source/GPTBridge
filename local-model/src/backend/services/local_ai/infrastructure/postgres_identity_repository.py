from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg

from .postgres_pool import PostgresPoolManager, get_pool_manager


class PostgresIdentityRepository:
    """PostgreSQL source of truth for the Xingcheng role identity.

    Governs the ``role_data``/``role_history``/``role_audit`` schemas (personality
    plus its version history and audit trail). At most one personality exists;
    every mutation records an immutable version and an audit event.
    """

    SCHEMAS = ("role_data", "role_history", "role_audit")

    def __init__(self, tool_root: Path, pool_manager: PostgresPoolManager | None = None):
        self.tool_root = Path(tool_root).resolve()
        self._manager = pool_manager or get_pool_manager(self.tool_root)

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        with self._manager.identity_pool().connection() as connection:
            yield connection

    def _require_identity_pool(self) -> None:
        # Resolve lazily so constructing the repository never fails when the
        # identity database is not configured; methods that touch it raise.
        if not (self._manager.identity_dsn or self._manager.identity_pool_available()):
            raise RuntimeError("GPTBRIDGE_XINGCHENG_IDENTITY_DSN_REQUIRED")

    def initialized(self) -> bool:
        pool = self._manager.identity_pool_or_none()
        if pool is None:
            return False
        with pool.connection() as connection:
            row = connection.execute(
                "SELECT to_regclass('role_data.personality') AS personality"
            ).fetchone()
        return bool(row and row.get("personality"))

    def personality(self, *, module_id: str = "local-ai") -> dict[str, Any] | None:
        self._require_identity_pool()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT resource_id, module_id, owner_id, resource_label,
                       classification, value, version, content_hash,
                       created_at, updated_at
                FROM role_data.personality
                WHERE module_id = %s
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
            "created_at": self._iso(row["created_at"]),
            "updated_at": self._iso(row["updated_at"]),
        }

    def personality_versions(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self._require_identity_pool()
        bounded = max(1, min(200, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resource_id, resource_label, value, version,
                       content_hash, created_at
                FROM role_history.personality_version
                ORDER BY version DESC LIMIT %s
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
                "created_at": self._iso(row["created_at"]),
            }
            for row in rows
        ]

    def save_personality(
        self,
        value: dict[str, Any],
        *,
        module_id: str = "local-ai",
        owner_id: str = "local-ai",
        classification: str = "private",
        resource_label: str = "",
    ) -> dict[str, Any]:
        self._require_identity_pool()
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
                    INSERT INTO role_data.personality (
                        resource_id, platform_id, module_id, owner_id,
                        data_category, resource_type, resource_label,
                        classification, value, version, content_hash
                    ) VALUES (%s, 'local-model-platform', %s, %s,
                              'role-setting', 'personality', %s, %s, %s::jsonb,
                              %s, %s)
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
                    UPDATE role_data.personality
                    SET value = %s::jsonb, version = %s, content_hash = %s,
                        resource_label = %s, classification = %s,
                        updated_at = now()
                    WHERE resource_id = %s
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
        self._require_identity_pool()
        bounded = max(1, min(200, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resource_id, owner_id, resource_label, value,
                       version, created_at
                FROM role_audit.event
                ORDER BY created_at DESC LIMIT %s
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
                "created_at": self._iso(row["created_at"]),
            }
            for row in rows
        ]

    def _append_version(
        self,
        connection: psycopg.Connection[dict[str, Any]],
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
            INSERT INTO role_history.personality_version (
                resource_id, platform_id, module_id, owner_id,
                data_category, resource_type, resource_label, classification,
                value, version, content_hash
            ) VALUES (%s, 'local-model-platform', %s, %s,
                      'role-setting', 'personality-version', %s, %s,
                      %s::jsonb, %s, %s)
            """,
            (
                resource_id, module_id, owner_id, label, classification,
                serialized, version, content_hash,
            ),
        )

    def _append_audit(
        self,
        connection: psycopg.Connection[dict[str, Any]],
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
            INSERT INTO role_audit.event (
                resource_id, platform_id, module_id, owner_id,
                data_category, resource_type, resource_label,
                classification, value, version, content_hash
            ) VALUES (%s, 'local-model-platform', %s, %s,
                      'role-setting-audit', 'audit-event', %s,
                      'private', %s::jsonb, 1, %s)
            """,
            (
                event_id, module_id, owner_id,
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

    @staticmethod
    def _iso(value: Any) -> str:
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)


__all__ = ["PostgresIdentityRepository"]
