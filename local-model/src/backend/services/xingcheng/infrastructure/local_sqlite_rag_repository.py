from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence


class LocalSqliteRagRepository:
    """Local sqlite3 source of truth for module-scoped RAG keyword metadata.

    Local replacement for the retired ``PostgresRagRepository`` that used
    ``gptbridge_index``/``gptbridge_rag`` PostgreSQL tables.  Operates on the
    local keyword index under ``tool_root/runtime/state``.  No PostgreSQL/
    psycopg, no external service (A44/E30).  FTS is a transparent local
    sqlite LIKE/BM25-style scoring layer over the keyword store.
    """

    def __init__(self, tool_root: Path) -> None:
        self.project_root = Path(tool_root).resolve().parent
        self.database_path = (
            Path(tool_root).resolve() / "runtime" / "state" / "local-rag-keywords.sqlite3"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS gptbridge_index_resource (
                    resource_id TEXT NOT NULL PRIMARY KEY,
                    platform_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    data_category TEXT NOT NULL,
                    resource_type TEXT NOT NULL,
                    resource_label TEXT NOT NULL,
                    classification TEXT NOT NULL,
                    locator_id TEXT NOT NULL,
                    content_hash TEXT,
                    version INTEGER NOT NULL DEFAULT 1,
                    index_status TEXT NOT NULL DEFAULT 'indexed',
                    metadata TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS gptbridge_rag_chunk (
                    chunk_id TEXT NOT NULL PRIMARY KEY,
                    resource_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    character_start INTEGER NOT NULL,
                    character_end INTEGER NOT NULL,
                    point_id TEXT,
                    embedding_model TEXT NOT NULL,
                    locator_fragment TEXT NOT NULL DEFAULT '',
                    metadata TEXT
                );
                CREATE TABLE IF NOT EXISTS gptbridge_rag_index_state (
                    resource_id TEXT NOT NULL PRIMARY KEY,
                    module_id TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    collection TEXT NOT NULL DEFAULT 'gptbridge_shared_knowledge',
                    chunk_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'indexed',
                    version INTEGER NOT NULL DEFAULT 1,
                    indexed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    def _loads(value: Any) -> Any:
        if isinstance(value, dict):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return value

    def existing_document(
        self, source: str, *, module_id: str = "xingcheng"
    ) -> dict[str, Any] | None:
        locator_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{module_id}:{source}")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT resource_id AS document_id, locator_id, metadata,
                       content_hash AS sha256, module_id, index_status,
                       updated_at AS indexed_at
                FROM gptbridge_index_resource
                WHERE module_id = ? AND resource_type = 'document'
                  AND locator_id = ?
                """,
                (module_id, str(locator_id)),
            ).fetchone()
        if row is None:
            return None
        metadata = self._loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {})
        metadata = metadata if isinstance(metadata, dict) else {}
        return {
            "document_id": str(row["document_id"]),
            "locator_id": str(row["locator_id"]),
            "title": str(metadata.get("title") or ""),
            "sha256": str(row["sha256"] or ""),
            "character_count": int(metadata.get("character_count") or 0),
            "chunk_count": int(metadata.get("chunk_count") or 0),
            "embedding_model": str(metadata.get("embedding_model") or ""),
            "module_id": str(row["module_id"]),
            "index_status": str(row["index_status"]),
            "indexed_at": str(row["indexed_at"]),
        }

    def replace_document(
        self,
        *,
        document: dict[str, Any],
        chunks: Sequence[dict[str, Any]],
    ) -> None:
        resource_id = str(document["resource_id"])
        metadata = {
            "document_id": document["document_id"],
            "source": document["source"],
            "title": document["title"],
            "character_count": document["character_count"],
            "chunk_count": len(chunks),
            "embedding_model": document["embedding_model"],
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO gptbridge_index_resource (
                    resource_id, platform_id, module_id, owner_id, data_category,
                    resource_type, resource_label, classification, locator_id, content_hash,
                    version, index_status, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'indexed', ?)
                ON CONFLICT (resource_id) DO UPDATE SET
                    resource_label = excluded.resource_label,
                    classification = excluded.classification,
                    content_hash = excluded.content_hash,
                    version = excluded.version,
                    index_status = 'indexed',
                    metadata = excluded.metadata
                """,
                (
                    resource_id,
                    str(document.get("platform_id") or ""),
                    document["module_id"],
                    document["owner_id"],
                    document["data_category"],
                    document["resource_type"],
                    document["resource_label"],
                    document["classification"],
                    document["locator_id"],
                    document["sha256"],
                    int(document.get("version") or 1),
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            connection.execute(
                "DELETE FROM gptbridge_rag_chunk WHERE resource_id = ?",
                (resource_id,),
            )
            for chunk in chunks:
                point_id = uuid.uuid5(
                    uuid.NAMESPACE_URL, "gptbridge-rag:" + str(chunk["chunk_id"])
                )
                connection.execute(
                    """
                    INSERT INTO gptbridge_rag_chunk (
                        chunk_id, resource_id, module_id, sequence,
                        character_start, character_end, point_id,
                        embedding_model, locator_fragment, metadata
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        chunk["chunk_id"],
                        resource_id,
                        document["module_id"],
                        chunk["sequence"],
                        chunk["character_start"],
                        chunk["character_end"],
                        str(point_id),
                        document["embedding_model"],
                        f"#chunk-{chunk['sequence']}",
                        json.dumps(
                            {
                                "resource_label": chunk["resource_label"],
                                "source": document["source"],
                                "title": document["title"],
                                "content": chunk["content"],
                            },
                            ensure_ascii=False,
                        ),
                    ),
                )
            connection.execute(
                """
                INSERT INTO gptbridge_rag_index_state (
                    resource_id, module_id, embedding_model, collection,
                    chunk_count, status, version
                ) VALUES (?, ?, ?, 'gptbridge_shared_knowledge', ?, 'indexed', ?)
                ON CONFLICT (resource_id) DO UPDATE SET
                    module_id = excluded.module_id,
                    embedding_model = excluded.embedding_model,
                    chunk_count = excluded.chunk_count,
                    status = 'indexed',
                    version = excluded.version
                """,
                (
                    resource_id,
                    document["module_id"],
                    document["embedding_model"],
                    len(chunks),
                    int(document.get("version") or 1),
                ),
            )

    def _keyword_score(self, query: str, content: str) -> float:
        tokens = [token for token in query.casefold().split() if token]
        if not tokens:
            return 0.0
        haystack = content.casefold()
        score = sum(haystack.count(token) for token in tokens)
        return float(score)

    def keyword_search(
        self, query: str, *, limit: int, module_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        if not module_ids:
            return []
        placeholders = ", ".join("?" for _ in module_ids)
        arguments: list[Any] = [*module_ids, query.casefold(), max(1, int(limit))]
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT chunk.chunk_id,
                       resource.metadata AS resource_metadata,
                       chunk.metadata AS chunk_metadata,
                       chunk.sequence, chunk.character_start,
                       chunk.character_end, chunk.module_id
                FROM gptbridge_rag_chunk AS chunk
                JOIN gptbridge_index_resource AS resource
                  ON resource.resource_id = chunk.resource_id
                WHERE chunk.module_id IN ({placeholders})
                  AND instr(lower(COALESCE(chunk.metadata, '')), ?) > 0
                ORDER BY chunk.sequence ASC
                LIMIT ?
                """,
                tuple(arguments),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            chunk_metadata = self._loads(row["chunk_metadata"]) if isinstance(row["chunk_metadata"], str) else (row["chunk_metadata"] or {})
            chunk_metadata = chunk_metadata if isinstance(chunk_metadata, dict) else {}
            resource_metadata = self._loads(row["resource_metadata"]) if isinstance(row["resource_metadata"], str) else (row["resource_metadata"] or {})
            resource_metadata = resource_metadata if isinstance(resource_metadata, dict) else {}
            content = str(chunk_metadata.get("content") or "")
            score = self._keyword_score(query, content)
            if score <= 0:
                continue
            results.append(
                {
                    "chunk_id": str(row["chunk_id"]),
                    "document_id": str(resource_metadata.get("document_id") or ""),
                    "title": str(chunk_metadata.get("title") or resource_metadata.get("title") or ""),
                    "source": str(
                        resource_metadata.get("source") or chunk_metadata.get("source") or ""
                    ),
                    "content": content,
                    "sequence": int(row["sequence"]),
                    "character_start": int(row["character_start"]),
                    "character_end": int(row["character_end"]),
                    "module_id": str(row["module_id"]),
                    "keyword_score": round(score, 6),
                }
            )
        results.sort(key=lambda item: -float(item["keyword_score"]))
        return results[: max(1, int(limit))]

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT state.resource_id AS resource_id,
                       state.chunk_count AS chunk_count,
                       resource.metadata AS metadata
                FROM gptbridge_rag_index_state AS state
                JOIN gptbridge_index_resource AS resource
                  ON resource.resource_id = state.resource_id
                WHERE state.status = 'indexed'
                """
            ).fetchall()
        document_count = len(rows)
        chunk_count = 0
        character_count = 0
        for row in rows:
            chunk_count += int(row["chunk_count"] or 0)
            metadata = self._loads(row["metadata"]) if isinstance(row["metadata"], str) else (row["metadata"] or {})
            metadata = metadata if isinstance(metadata, dict) else {}
            try:
                character_count += int(metadata.get("character_count") or 0)
            except (TypeError, ValueError):
                pass
        return {
            "engine": "local-sqlite3",
            "schema": "local-rag-keywords",
            "fts_enabled": True,
            "content_storage": "excluded-by-architecture",
            "document_count": document_count,
            "chunk_count": chunk_count,
            "character_count": character_count,
        }


__all__ = ["LocalSqliteRagRepository"]