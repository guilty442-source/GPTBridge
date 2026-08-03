from __future__ import annotations

import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row


class PostgresRagRepository:
    """PostgreSQL source of truth for module-scoped RAG metadata."""

    def __init__(self, tool_root: Path) -> None:
        self.project_root = Path(tool_root).resolve().parent
        self.dsn = str(os.environ.get("GPTBRIDGE_POSTGRES_DSN") or "").strip()
        if not self.dsn:
            raise RuntimeError("GPTBRIDGE_POSTGRES_DSN_REQUIRED")
        self.pool = ConnectionPool(
            conninfo=self.dsn,
            min_size=1,
            max_size=8,
            kwargs={"row_factory": dict_row},
            open=True,
        )
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        with self.pool.connection() as connection:
            yield connection

    def close(self) -> None:
        self.pool.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT to_regclass('gptbridge_index.resource') AS resource, "
                "to_regclass('gptbridge_rag.chunk') AS chunk"
            ).fetchone()
        if not row or row.get("resource") is None or row.get("chunk") is None:
            raise RuntimeError("CENTRAL_INDEX_NOT_PROVISIONED")

    def existing_document(
        self, source: str, *, module_id: str = "local-ai"
    ) -> dict[str, Any] | None:
        locator_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{module_id}:{source}")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT resource_id AS document_id, locator_id,
                       metadata->>'title' AS title, content_hash AS sha256,
                       COALESCE((metadata->>'character_count')::bigint, 0) AS character_count,
                       COALESCE((metadata->>'chunk_count')::bigint, 0) AS chunk_count,
                       metadata->>'embedding_model' AS embedding_model,
                       updated_at AS indexed_at, module_id
                FROM gptbridge_index.resource
                WHERE module_id = %s AND resource_type = 'document'
                  AND locator_id = %s
                """,
                (module_id, locator_id),
            ).fetchone()
        return dict(row) if row else None

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
                INSERT INTO gptbridge_index.resource (
                    resource_id, platform_id, module_id, owner_id, data_category,
                    resource_type, resource_label, classification, locator_id, content_hash,
                    version, index_status, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'indexed', %s)
                ON CONFLICT (resource_id) DO UPDATE SET
                    resource_label = EXCLUDED.resource_label,
                    classification = EXCLUDED.classification,
                    content_hash = EXCLUDED.content_hash,
                    version = EXCLUDED.version,
                    index_status = 'indexed', metadata = EXCLUDED.metadata,
                    updated_at = now()
                """,
                (
                    resource_id, document["platform_id"], document["module_id"],
                    document["owner_id"], document["data_category"],
                    document["resource_type"], document["resource_label"],
                    document["classification"], document["locator_id"],
                    document["sha256"],
                    document["version"], json.dumps(metadata, ensure_ascii=False),
                ),
            )
            connection.execute(
                "DELETE FROM gptbridge_rag.chunk WHERE resource_id = %s",
                (resource_id,),
            )
            for chunk in chunks:
                point_id = uuid.uuid5(
                    uuid.NAMESPACE_URL, "gptbridge-rag:" + str(chunk["chunk_id"])
                )
                connection.execute(
                    """
                    INSERT INTO gptbridge_rag.chunk (
                        chunk_id, resource_id, module_id, sequence,
                        character_start, character_end, qdrant_point_id,
                        embedding_model, locator_fragment, metadata
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        chunk["chunk_id"], resource_id, document["module_id"],
                        chunk["sequence"], chunk["character_start"],
                        chunk["character_end"], point_id,
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
                INSERT INTO gptbridge_rag.index_state (
                    resource_id, module_id, embedding_model, qdrant_collection,
                    chunk_count, status, version, indexed_at
                ) VALUES (%s, %s, %s, 'gptbridge_shared_knowledge', %s, 'indexed', %s, now())
                ON CONFLICT (resource_id) DO UPDATE SET
                    module_id = EXCLUDED.module_id,
                    embedding_model = EXCLUDED.embedding_model,
                    chunk_count = EXCLUDED.chunk_count,
                    status = 'indexed', version = EXCLUDED.version,
                    indexed_at = now(), updated_at = now()
                """,
                (
                    resource_id, document["module_id"],
                    document["embedding_model"], len(chunks), document["version"],
                ),
            )

    def keyword_search(
        self, query: str, *, limit: int, module_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        if not module_ids:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT chunk.chunk_id,
                       resource.metadata->>'document_id' AS document_id,
                       chunk.metadata->>'title' AS title,
                       COALESCE(
                           resource.metadata->>'source',
                           chunk.metadata->>'source'
                       ) AS source,
                       chunk.metadata->>'content' AS content,
                       chunk.sequence, chunk.character_start,
                       chunk.character_end, chunk.module_id,
                       ts_rank_cd(
                           to_tsvector('simple', COALESCE(chunk.metadata->>'content', '')),
                           websearch_to_tsquery('simple', %s)
                       )
                           AS keyword_score
                FROM gptbridge_rag.chunk AS chunk
                JOIN gptbridge_index.resource AS resource
                  ON resource.resource_id = chunk.resource_id
                WHERE chunk.module_id = ANY(%s)
                  AND to_tsvector(
                      'simple', COALESCE(chunk.metadata->>'content', '')
                  ) @@ websearch_to_tsquery('simple', %s)
                ORDER BY keyword_score DESC
                LIMIT %s
                """,
                (query, list(module_ids), query, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def status(self) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS document_count,
                       COALESCE(SUM(state.chunk_count), 0) AS chunk_count,
                       COALESCE(
                           SUM(
                               CASE
                                   WHEN resource.metadata->>'character_count' ~ '^[0-9]+$'
                                   THEN (resource.metadata->>'character_count')::bigint
                                   ELSE 0
                               END
                           ),
                           0
                       ) AS character_count
                FROM gptbridge_rag.index_state AS state
                JOIN gptbridge_index.resource AS resource
                  ON resource.resource_id = state.resource_id
                WHERE state.status = 'indexed'
                """
            ).fetchone() or {}
        return {
            "engine": "postgresql",
            "schema": "gptbridge_rag",
            "fts_enabled": True,
            "content_storage": "excluded-by-architecture",
            "document_count": int(row.get("document_count") or 0),
            "chunk_count": int(row.get("chunk_count") or 0),
            "character_count": int(row.get("character_count") or 0),
        }


__all__ = ["PostgresRagRepository"]
