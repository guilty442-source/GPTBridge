from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


class LocalSqliteCognitionRepository:
    """Local sqlite3 source of truth for the cognition module.

    Local replacement for the retired ``PostgresCognitionRepository`` that used
    the ``cognition`` PostgreSQL schema.  Operates on the local cognition
    store under ``tool_root/runtime/state``.  No PostgreSQL/psycopg, no
    external service (A44/E30).
    """

    def __init__(self, tool_root: Path) -> None:
        self.fallback = "local-cognition"
        self.database_path = (
            Path(tool_root).resolve() / "runtime" / "state" / "cognition.sqlite3"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cognition_model_data (
                    model_data_id TEXT NOT NULL PRIMARY KEY,
                    platform_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    publisher TEXT NOT NULL,
                    model_data_name TEXT NOT NULL,
                    model_data_version TEXT NOT NULL,
                    data_category TEXT NOT NULL DEFAULT 'model-weights',
                    data_classification TEXT NOT NULL DEFAULT 'internal',
                    model_store_path TEXT,
                    location_type TEXT NOT NULL DEFAULT 'local',
                    endpoint TEXT,
                    status TEXT NOT NULL DEFAULT 'ready',
                    metadata TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cognition_knowledge (
                    knowledge_id TEXT NOT NULL PRIMARY KEY,
                    platform_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    knowledge_name TEXT NOT NULL,
                    knowledge_category TEXT NOT NULL,
                    data_classification TEXT NOT NULL DEFAULT 'internal',
                    source_path TEXT,
                    status TEXT NOT NULL DEFAULT 'ready',
                    metadata TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cognition_model_capability (
                    capability_id TEXT NOT NULL PRIMARY KEY,
                    platform_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    capability_name TEXT NOT NULL,
                    capability_version TEXT NOT NULL,
                    modality TEXT NOT NULL,
                    capability_engine TEXT NOT NULL,
                    model_data_id TEXT,
                    status TEXT NOT NULL DEFAULT 'enabled',
                    metadata TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS cognition_rag_reference (
                    reference_id TEXT NOT NULL PRIMARY KEY,
                    platform_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    collection TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    chunk_id TEXT NOT NULL,
                    point_id TEXT,
                    source TEXT,
                    title TEXT,
                    knowledge_id TEXT,
                    status TEXT NOT NULL DEFAULT 'referenced',
                    metadata TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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

    def close(self) -> None:
        return None

    @staticmethod
    def _new_id(namespace: str, *parts: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, ":".join((namespace, *parts))))

    @staticmethod
    def _loads(value: Any) -> Any:
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    def save_model_data(self, *, item: dict[str, Any]) -> dict[str, Any]:
        item_id = str(item["model_data_id"])
        metadata = item.get("metadata") or {}
        columns = (
            "platform_id", "owner_id", "publisher", "model_data_name",
            "model_data_version", "data_category", "data_classification",
            "model_store_path", "location_type", "endpoint", "status", "metadata",
        )
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO cognition_model_data (
                    model_data_id, {", ".join(columns)}
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (model_data_id) DO UPDATE SET
                    platform_id = excluded.platform_id,
                    owner_id = excluded.owner_id,
                    publisher = excluded.publisher,
                    model_data_name = excluded.model_data_name,
                    model_data_version = excluded.model_data_version,
                    data_category = excluded.data_category,
                    data_classification = excluded.data_classification,
                    model_store_path = excluded.model_store_path,
                    location_type = excluded.location_type,
                    endpoint = excluded.endpoint,
                    status = excluded.status,
                    metadata = excluded.metadata
                """,
                (
                    item_id,
                    item.get("platform_id") or "",
                    item.get("owner_id") or "",
                    item.get("publisher") or "",
                    item.get("model_data_name") or "",
                    item.get("model_data_version") or "",
                    item.get("data_category") or "model-weights",
                    item.get("data_classification") or "internal",
                    item.get("model_store_path") or "",
                    item.get("location_type") or "local",
                    item.get("endpoint") or "",
                    item.get("status") or "ready",
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
        return {"model_data_id": item_id, "status": "saved"}

    def load_model_data(self, *, model_data_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cognition_model_data WHERE model_data_id = ?",
                (model_data_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_model_data(row)

    def list_model_data(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if owner_id:
                rows = connection.execute(
                    "SELECT * FROM cognition_model_data WHERE owner_id = ? ORDER BY model_data_name ASC",
                    (owner_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM cognition_model_data ORDER BY model_data_name ASC"
                ).fetchall()
        return [self._row_model_data(row) for row in rows]

    @staticmethod
    def _row_model_data(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "model_data_id": str(row["model_data_id"]),
            "platform_id": str(row["platform_id"]),
            "owner_id": str(row["owner_id"]),
            "publisher": str(row["publisher"]),
            "model_data_name": str(row["model_data_name"]),
            "model_data_version": str(row["model_data_version"]),
            "data_category": str(row["data_category"]),
            "data_classification": str(row["data_classification"]),
            "model_store_path": str(row["model_store_path"] or ""),
            "location_type": str(row["location_type"]),
            "endpoint": str(row["endpoint"] or ""),
            "status": str(row["status"]),
            "metadata": row["metadata"],
        }

    def save_knowledge(self, *, item: dict[str, Any]) -> dict[str, Any]:
        item_id = str(item["knowledge_id"])
        columns = (
            "platform_id", "owner_id", "knowledge_name", "knowledge_category",
            "data_classification", "source_path", "status", "metadata",
        )
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO cognition_knowledge (
                    knowledge_id, {", ".join(columns)}
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (knowledge_id) DO UPDATE SET
                    platform_id = excluded.platform_id,
                    owner_id = excluded.owner_id,
                    knowledge_name = excluded.knowledge_name,
                    knowledge_category = excluded.knowledge_category,
                    data_classification = excluded.data_classification,
                    source_path = excluded.source_path,
                    status = excluded.status,
                    metadata = excluded.metadata
                """,
                (
                    item_id,
                    item.get("platform_id") or "",
                    item.get("owner_id") or "",
                    item.get("knowledge_name") or "",
                    item.get("knowledge_category") or "",
                    item.get("data_classification") or "internal",
                    item.get("source_path") or "",
                    item.get("status") or "ready",
                    json.dumps(item.get("metadata") or {}, ensure_ascii=False),
                ),
            )
        return {"knowledge_id": item_id, "status": "saved"}

    def load_knowledge(self, *, knowledge_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cognition_knowledge WHERE knowledge_id = ?",
                (knowledge_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_knowledge(row)

    def list_knowledge(self, *, owner_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if owner_id:
                rows = connection.execute(
                    "SELECT * FROM cognition_knowledge WHERE owner_id = ? ORDER BY knowledge_name ASC",
                    (owner_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM cognition_knowledge ORDER BY knowledge_name ASC"
                ).fetchall()
        return [self._row_knowledge(row) for row in rows]

    @staticmethod
    def _row_knowledge(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "knowledge_id": str(row["knowledge_id"]),
            "platform_id": str(row["platform_id"]),
            "owner_id": str(row["owner_id"]),
            "knowledge_name": str(row["knowledge_name"]),
            "knowledge_category": str(row["knowledge_category"]),
            "data_classification": str(row["data_classification"]),
            "source_path": str(row["source_path"] or ""),
            "status": str(row["status"]),
            "metadata": row["metadata"],
        }

    def save_model_capability(self, *, item: dict[str, Any]) -> dict[str, Any]:
        item_id = str(item["capability_id"])
        columns = (
            "platform_id", "owner_id", "capability_name", "capability_version",
            "modality", "capability_engine", "model_data_id", "status", "metadata",
        )
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO cognition_model_capability (
                    capability_id, {", ".join(columns)}
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (capability_id) DO UPDATE SET
                    platform_id = excluded.platform_id,
                    owner_id = excluded.owner_id,
                    capability_name = excluded.capability_name,
                    capability_version = excluded.capability_version,
                    modality = excluded.modality,
                    capability_engine = excluded.capability_engine,
                    model_data_id = excluded.model_data_id,
                    status = excluded.status,
                    metadata = excluded.metadata
                """,
                (
                    item_id,
                    item.get("platform_id") or "",
                    item.get("owner_id") or "",
                    item.get("capability_name") or "",
                    item.get("capability_version") or "",
                    item.get("modality") or "text",
                    item.get("capability_engine") or "",
                    item.get("model_data_id") or "",
                    item.get("status") or "enabled",
                    json.dumps(item.get("metadata") or {}, ensure_ascii=False),
                ),
            )
        return {"capability_id": item_id, "status": "saved"}

    def load_model_capability(self, *, capability_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cognition_model_capability WHERE capability_id = ?",
                (capability_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_capability(row)

    def list_model_capabilities(
        self, *, owner_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if owner_id:
                rows = connection.execute(
                    "SELECT * FROM cognition_model_capability WHERE owner_id = ? ORDER BY capability_name ASC",
                    (owner_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM cognition_model_capability ORDER BY capability_name ASC"
                ).fetchall()
        return [self._row_capability(row) for row in rows]

    @staticmethod
    def _row_capability(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "capability_id": str(row["capability_id"]),
            "platform_id": str(row["platform_id"]),
            "owner_id": str(row["owner_id"]),
            "capability_name": str(row["capability_name"]),
            "capability_version": str(row["capability_version"]),
            "modality": str(row["modality"]),
            "capability_engine": str(row["capability_engine"]),
            "model_data_id": str(row["model_data_id"] or ""),
            "status": str(row["status"]),
            "metadata": row["metadata"],
        }

    def register_rag_reference(
        self,
        *,
        module_id: str,
        owner_id: str,
        collection: str,
        document_id: str,
        chunk_id: str,
        point_id: str,
        source: str | None = None,
        title: str | None = None,
        knowledge_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reference_id = self._new_id(module_id, document_id, chunk_id)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO cognition_rag_reference (
                    reference_id, platform_id, module_id, owner_id, collection,
                    document_id, chunk_id, point_id, source, title, knowledge_id,
                    status, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'referenced', ?)
                ON CONFLICT (reference_id) DO UPDATE SET
                    collection = excluded.collection,
                    point_id = excluded.point_id,
                    source = excluded.source,
                    title = excluded.title,
                    status = 'referenced'
                """,
                (
                    reference_id,
                    "local",
                    module_id,
                    owner_id,
                    collection,
                    document_id,
                    chunk_id,
                    point_id,
                    source or "",
                    title or "",
                    knowledge_id or "",
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
        return {"reference_id": reference_id, "status": "referenced"}

    def rag_references(
        self, *, module_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._connect() as connection:
            if module_id:
                rows = connection.execute(
                    "SELECT * FROM cognition_rag_reference "
                    "WHERE module_id = ? ORDER BY created_at DESC LIMIT ?",
                    (module_id, max(1, int(limit))),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM cognition_rag_reference "
                    "ORDER BY created_at DESC LIMIT ?",
                    (max(1, int(limit)),),
                ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "reference_id": str(row["reference_id"]),
                "platform_id": str(row["platform_id"]),
                "module_id": str(row["module_id"]),
                "owner_id": str(row["owner_id"]),
                "collection": str(row["collection"]),
                "document_id": str(row["document_id"]),
                "chunk_id": str(row["chunk_id"]),
                "point_id": str(row["point_id"] or ""),
                "source": str(row["source"] or ""),
                "title": str(row["title"] or ""),
                "knowledge_id": str(row["knowledge_id"] or ""),
                "status": str(row["status"]),
                "metadata": row["metadata"],
            }
            results.append(item)
        return results

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            model_data = connection.execute(
                "SELECT COUNT(*) AS c FROM cognition_model_data"
            ).fetchone()["c"]
            knowledge = connection.execute(
                "SELECT COUNT(*) AS c FROM cognition_knowledge"
            ).fetchone()["c"]
            capabilities = connection.execute(
                "SELECT COUNT(*) AS c FROM cognition_model_capability"
            ).fetchone()["c"]
            references = connection.execute(
                "SELECT COUNT(*) AS c FROM cognition_rag_reference"
            ).fetchone()["c"]
        return {
            "engine": "local-sqlite3-degraded",
            "role": "owner-private-state-cache-checkpoint-or-bounded-reconciled-degraded-transport-only",
            "canonical_central_engine": "postgresql",
            "canonical": False,
            "authority": "non-canonical-reconciliation-required",
            "reconciliation_required": True,
            "schema": "cognition",
            "model_data_count": int(model_data),
            "knowledge_count": int(knowledge),
            "capability_count": int(capabilities),
            "rag_reference_count": int(references),
        }


__all__ = ["LocalSqliteCognitionRepository"]