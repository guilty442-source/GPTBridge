from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import psycopg

from .postgres_pool import PostgresPoolManager, get_pool_manager


class PostgresCognitionRepository:
    """PostgreSQL source of truth for Xingcheng model cognition.

    Governs the ``cognition`` schema: model data, curated knowledge, model
    capability settings and their RAG references.
    """

    SCHEMAS = ("cognition",)

    def __init__(self, tool_root: Path, pool_manager: PostgresPoolManager | None = None):
        self.tool_root = Path(tool_root).resolve()
        self._manager = pool_manager or get_pool_manager(self.tool_root)

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        with self._manager.cognition_pool().connection() as connection:
            yield connection

    def initialized(self) -> bool:
        pool = None
        try:
            pool = self._manager.cognition_pool()
        except RuntimeError:
            return False
        with pool.connection() as connection:
            row = connection.execute(
                "SELECT to_regclass('cognition.model_data') AS model_data"
            ).fetchone()
        return bool(row and row.get("model_data"))

    # ----------------------------------------------------------------- model --
    def save_model_data(
        self,
        *,
        module_id: str = "local-ai",
        owner_id: str = "local-ai",
        data_type: str,
        value: dict[str, Any],
        resource_label: str = "",
    ) -> dict[str, Any]:
        return self._upsert_resource(
            table="cognition.model_data",
            column="data_type",
            resource_id_prefix="cognition-model",
            module_id=module_id,
            owner_id=owner_id,
            kind=data_type,
            value=value,
            resource_label=resource_label,
        )

    def load_model_data(
        self,
        resource_id: str,
        *,
        module_id: str = "local-ai",
    ) -> dict[str, Any] | None:
        return self._load_resource(
            "cognition.model_data", resource_id, module_id=module_id
        )

    def list_model_data(
        self, *, module_id: str = "local-ai", data_type: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        return self._list_resources(
            "cognition.model_data",
            kind_column="data_type",
            module_id=module_id,
            kind=data_type,
            limit=limit,
        )

    # ------------------------------------------------------------- knowledge --
    def save_knowledge(
        self,
        *,
        module_id: str = "local-ai",
        owner_id: str = "local-ai",
        knowledge_type: str,
        value: dict[str, Any],
        classification: str = "private",
        resource_label: str = "",
    ) -> dict[str, Any]:
        return self._upsert_resource(
            table="cognition.knowledge",
            column="knowledge_type",
            resource_id_prefix="cognition-knowledge",
            module_id=module_id,
            owner_id=owner_id,
            kind=knowledge_type,
            value=value,
            resource_label=resource_label,
            extra={"classification": classification},
        )

    def load_knowledge(
        self, resource_id: str, *, module_id: str = "local-ai"
    ) -> dict[str, Any] | None:
        return self._load_resource("cognition.knowledge", resource_id, module_id=module_id)

    def list_knowledge(
        self, *, module_id: str = "local-ai", knowledge_type: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        return self._list_resources(
            "cognition.knowledge",
            kind_column="knowledge_type",
            module_id=module_id,
            kind=knowledge_type,
            limit=limit,
        )

    # ------------------------------------------------------------ capability --
    def save_model_capability(
        self,
        *,
        module_id: str = "local-ai",
        model_id: str,
        capability_id: str,
        settings: dict[str, Any] | None = None,
        evaluation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = settings or {}
        evaluation = evaluation or {}
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cognition.model_capability (
                    model_id, capability_id, settings, evaluation, updated_at
                ) VALUES (%s, %s, %s::jsonb, %s::jsonb, now())
                ON CONFLICT (model_id, capability_id) DO UPDATE SET
                    settings = EXCLUDED.settings,
                    evaluation = EXCLUDED.evaluation,
                    updated_at = now()
                """,
                (
                    model_id,
                    capability_id,
                    json.dumps(settings, ensure_ascii=False, sort_keys=True),
                    json.dumps(evaluation, ensure_ascii=False, sort_keys=True),
                ),
            )
        return {
            "ok": True,
            "model_id": model_id,
            "capability_id": capability_id,
            "module_id": module_id,
            "settings": settings,
        }

    def load_model_capability(
        self, *, model_id: str, capability_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT model_id, capability_id, settings, evaluation, version, updated_at
                FROM cognition.model_capability
                WHERE model_id = %s AND capability_id = %s
                """,
                (model_id, capability_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "model_id": str(row["model_id"]),
            "capability_id": str(row["capability_id"]),
            "settings": self._loads(row["settings"]),
            "evaluation": self._loads(row["evaluation"]),
            "version": int(row["version"]),
            "updated_at": self._iso(row["updated_at"]),
        }

    def list_model_capabilities(self, *, model_id: str, limit: int = 100) -> list[dict[str, Any]]:
        bounded = max(1, min(200, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT model_id, capability_id, settings, evaluation, version, updated_at
                FROM cognition.model_capability
                WHERE model_id = %s
                ORDER BY capability_id ASC LIMIT %s
                """,
                (model_id, bounded),
            ).fetchall()
        return [
            {
                "model_id": str(row["model_id"]),
                "capability_id": str(row["capability_id"]),
                "settings": self._loads(row["settings"]),
                "evaluation": self._loads(row["evaluation"]),
                "version": int(row["version"]),
                "updated_at": self._iso(row["updated_at"]),
            }
            for row in rows
        ]

    # --------------------------------------------------------- rag reference --
    def register_rag_reference(
        self,
        *,
        resource_id: str,
        central_resource_id: str,
        module_id: str = "local-ai",
        qdrant_point_id: str = "",
    ) -> dict[str, Any]:
        point = uuid.UUID(str(qdrant_point_id or uuid.uuid4().hex)) if qdrant_point_id else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cognition.rag_reference (
                    resource_id, central_resource_id, qdrant_point_id, module_id
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (resource_id, central_resource_id) DO UPDATE SET
                    qdrant_point_id = EXCLUDED.qdrant_point_id,
                    module_id = EXCLUDED.module_id
                """,
                (resource_id, central_resource_id, point, module_id),
            )
        return {
            "ok": True,
            "resource_id": resource_id,
            "central_resource_id": central_resource_id,
            "module_id": module_id,
            "qdrant_point_id": str(point) if point else None,
        }

    def rag_references(self, *, resource_id: str, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(200, int(limit)))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT resource_id, central_resource_id, qdrant_point_id,
                       module_id, version
                FROM cognition.rag_reference
                WHERE resource_id = %s
                ORDER BY version DESC LIMIT %s
                """,
                (resource_id, bounded),
            ).fetchall()
        return [
            {
                "resource_id": str(row["resource_id"]),
                "central_resource_id": str(row["central_resource_id"]),
                "qdrant_point_id": str(row["qdrant_point_id"]) if row["qdrant_point_id"] else None,
                "module_id": str(row["module_id"]),
                "version": int(row["version"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------ shared impl --
    def _upsert_resource(
        self,
        *,
        table: str,
        column: str,
        resource_id_prefix: str,
        module_id: str,
        owner_id: str,
        kind: str,
        value: dict[str, Any],
        resource_label: str = "",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
        content_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        label = str(resource_label or f"星澄{kind}").strip()[:255]
        resource_id = (
            f"{resource_id_prefix}:{module_id}:{content_hash[:24]}"
        )
        with self._connect() as connection:
            current = self._load_resource_connection(
                connection, table, resource_id, module_id=module_id
            )
            if current is None:
                columns = (
                    "resource_id, platform_id, module_id, owner_id, resource_label, "
                    f"{column}, value, version, content_hash"
                )
                placeholders = (
                    "%s, 'local-model-platform', %s, %s, %s, %s, %s::jsonb, 1, %s"
                )
                connection.execute(
                    f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                    (resource_id, module_id, owner_id, label, kind, serialized, content_hash),
                )
                version = 1
            else:
                version = int(current["version"]) + 1
                connection.execute(
                    f"""
                    UPDATE {table}
                    SET {column} = %s, value = %s::jsonb, version = %s,
                        content_hash = %s, resource_label = %s,
                        updated_at = now()
                    WHERE resource_id = %s
                    """,
                    (kind, serialized, version, content_hash, label, resource_id),
                )
        result = {
            "ok": True,
            "resource_id": resource_id,
            "resource_label": label,
            "kind": kind,
            "version": version,
            "content_hash": content_hash,
            "module_id": module_id,
            "owner_id": owner_id,
        }
        if extra:
            result.update(extra)
        return result

    def _load_resource(
        self, table: str, resource_id: str, *, module_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as connection:
            return self._load_resource_connection(
                connection, table, resource_id, module_id=module_id
            )

    @staticmethod
    def _load_resource_connection(
        connection: psycopg.Connection[dict[str, Any]],
        table: str,
        resource_id: str,
        *,
        module_id: str,
    ) -> dict[str, Any] | None:
        row = connection.execute(
            f"""
            SELECT resource_id, module_id, owner_id, resource_label,
                   value, version, content_hash, updated_at
            FROM {table}
            WHERE resource_id = %s AND module_id = %s
            """,
            (resource_id, module_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "resource_id": str(row["resource_id"]),
            "module_id": str(row["module_id"]),
            "owner_id": str(row["owner_id"]),
            "resource_label": str(row["resource_label"]),
            "value": PostgresCognitionRepository._loads(row["value"]),
            "version": int(row["version"]),
            "content_hash": str(row["content_hash"]),
            "updated_at": PostgresCognitionRepository._iso(row["updated_at"]),
        }

    def _list_resources(
        self, table: str, *, kind_column: str, module_id: str, kind: str, limit: int
    ) -> list[dict[str, Any]]:
        bounded = max(1, min(200, int(limit)))
        clauses = ["module_id = %s"]
        arguments: list[Any] = [module_id]
        if kind:
            clauses.append(f"{kind_column} = %s")
            arguments.append(kind)
        arguments.append(bounded)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT resource_id, module_id, owner_id, resource_label,
                       value, version, content_hash, updated_at
                FROM {table}
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC LIMIT %s
                """,
                arguments,
            ).fetchall()
        return [
            {
                "resource_id": str(row["resource_id"]),
                "module_id": str(row["module_id"]),
                "owner_id": str(row["owner_id"]),
                "resource_label": str(row["resource_label"]),
                "value": self._loads(row["value"]),
                "version": int(row["version"]),
                "content_hash": str(row["content_hash"]),
                "updated_at": self._iso(row["updated_at"]),
            }
            for row in rows
        ]

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


__all__ = ["PostgresCognitionRepository"]
