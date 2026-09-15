"""RAG canonical document/chunk writes and PostgreSQL FTS reads (A371-A374).

A374 binding order step 2: PostgreSQL is the live authority for official
metadata, FTS and index_state.  These methods are mixed into
``PostgreSQLMetadataAuthority`` so the document write path
(resource + chunks + index_state) and the keyword channel share the same
managed connection.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional, Sequence

_logger = logging.getLogger("gptbridge.rag")


_RESOURCE_UPSERT_SQL = """INSERT INTO gptbridge_index.resource
      (resource_id, platform_id, module_id, owner_id,
       data_category, resource_type, resource_label,
       classification, locator_id, content_hash, version,
       index_status, metadata, logical_key)
   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'indexed',%s,%s)
   ON CONFLICT (resource_id) DO UPDATE SET
       resource_label = EXCLUDED.resource_label,
       classification = EXCLUDED.classification,
       content_hash = EXCLUDED.content_hash,
       version = EXCLUDED.version,
       index_status = 'indexed',
       metadata = EXCLUDED.metadata,
       updated_at = now()"""

_CHUNK_INSERT_SQL = """INSERT INTO gptbridge_rag.chunk
      (chunk_id, resource_id, module_id, sequence,
       character_start, character_end, qdrant_point_id,
       embedding_model, locator_fragment, metadata)
   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"""


def _resource_params(document: dict[str, Any]) -> tuple:
    """INSERT params for gptbridge_index.resource."""
    metadata = {
        "document_id": document.get("document_id"),
        "source": document.get("source"),
        "title": document.get("title"),
        "character_count": document.get("character_count"),
        "chunk_count": document.get("chunk_count"),
        "embedding_model": document.get("embedding_model"),
    }
    label = str(document["resource_label"])
    return (
        str(document["resource_id"]),
        str(document.get("platform_id") or ""),
        str(document["module_id"]),
        str(document["owner_id"]),
        str(document["data_category"]),
        str(document["resource_type"]),
        label,
        str(document.get("classification") or "private"),
        str(document["locator_id"]),
        str(document.get("sha256") or document.get("content_hash") or ""),
        int(document.get("version") or 1),
        json.dumps(metadata, ensure_ascii=False),
        label,
    )


def _chunk_params(
    chunk: dict[str, Any],
    resource_id: str,
    module_id: str,
    embedding_model: str,
) -> tuple:
    """INSERT params for gptbridge_rag.chunk."""
    point_id = chunk.get("qdrant_point_id") or chunk.get("point_id")
    metadata = {
        "resource_label": chunk.get("resource_label"),
        "source": chunk.get("source"),
        "title": chunk.get("title"),
        "content": chunk.get("content"),
    }
    return (
        str(chunk["chunk_id"]),
        resource_id,
        module_id,
        int(chunk["sequence"]),
        int(chunk["character_start"]),
        int(chunk["character_end"]),
        str(uuid.UUID(str(point_id))) if point_id else None,
        embedding_model,
        str(chunk.get("locator_fragment") or f"#chunk-{chunk['sequence']}"),
        json.dumps(metadata, ensure_ascii=False),
    )


class RagMetadataDocumentsMixin:
    """Document-level canonical writes and FTS keyword search."""

    async def ensure_resource(self, document: dict[str, Any]) -> bool:
        """Upsert the document row in gptbridge_index.resource."""
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(_RESOURCE_UPSERT_SQL, _resource_params(document))
            return True
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: ensure_resource failed: %s", exc
            )
            return False

    async def replace_document_chunks(
        self,
        *,
        resource_id: str,
        module_id: str,
        embedding_model: str,
        chunks: Sequence[dict[str, Any]],
    ) -> bool:
        """Replace all chunk rows for a document (A374 canonical write)."""
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM gptbridge_rag.chunk WHERE resource_id = %s",
                    (resource_id,),
                )
                for chunk in chunks:
                    await cur.execute(
                        _CHUNK_INSERT_SQL,
                        _chunk_params(chunk, resource_id, module_id, embedding_model),
                    )
            return True
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: replace_document_chunks failed: %s",
                exc,
            )
            return False

    async def fetch_document(
        self, module_id: str, resource_id: str
    ) -> Optional[dict[str, Any]]:
        """Fetch a document resource for ingest deduplication."""
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT resource_id, content_hash, index_status, metadata,
                              updated_at
                       FROM gptbridge_index.resource
                       WHERE module_id = %s AND resource_id = %s
                         AND resource_type = 'document'""",
                    (module_id, resource_id),
                )
                row = await cur.fetchone()
            if row is None:
                return None
            metadata = row[3] if isinstance(row[3], dict) else {}
            return {
                "document_id": str(metadata.get("document_id") or ""),
                "resource_id": str(row[0]),
                "sha256": str(row[1] or ""),
                "index_status": str(row[2] or ""),
                "title": str(metadata.get("title") or ""),
                "character_count": int(metadata.get("character_count") or 0),
                "chunk_count": int(metadata.get("chunk_count") or 0),
                "embedding_model": str(metadata.get("embedding_model") or ""),
                "module_id": module_id,
                "indexed_at": str(row[4] or ""),
            }
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: fetch_document failed: %s", exc
            )
            return None

    async def keyword_search(
        self,
        query: str,
        *,
        module_ids: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        """PostgreSQL FTS keyword channel (A374 step 2, live authority)."""
        if not self._healthy or not self._conn or not module_ids:
            return []
        try:
            placeholders = ",".join(["%s"] * len(module_ids))
            async with self._conn.cursor() as cur:
                await cur.execute(
                    f"""SELECT chunk.chunk_id, chunk.resource_id, chunk.module_id,
                               chunk.sequence, chunk.character_start,
                               chunk.character_end,
                               ts_rank(chunk.content_tsv,
                                       plainto_tsquery('simple', %s)) AS rank,
                               chunk.metadata AS chunk_metadata,
                               resource.metadata AS resource_metadata
                        FROM gptbridge_rag.chunk AS chunk
                        JOIN gptbridge_index.resource AS resource
                          ON resource.resource_id = chunk.resource_id
                        WHERE chunk.module_id IN ({placeholders})
                          AND chunk.content_tsv @@ plainto_tsquery('simple', %s)
                        ORDER BY rank DESC
                        LIMIT %s""",
                    (query, *module_ids, query, max(1, int(limit))),
                )
                rows = await cur.fetchall()
            return [self._keyword_row(row) for row in rows]
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: keyword_search failed: %s", exc
            )
            return []

    @staticmethod
    def _keyword_row(row: Any) -> dict[str, Any]:
        chunk_meta = row[7] if isinstance(row[7], dict) else {}
        resource_meta = row[8] if isinstance(row[8], dict) else {}
        return {
            "chunk_id": str(row[0]),
            "document_id": str(resource_meta.get("document_id") or ""),
            "resource_id": str(row[1]),
            "document_resource_id": str(row[1]),
            "module_id": str(row[2]),
            "sequence": int(row[3]),
            "character_start": int(row[4]),
            "character_end": int(row[5]),
            "title": str(
                chunk_meta.get("title") or resource_meta.get("title") or ""
            ),
            "source": str(
                resource_meta.get("source") or chunk_meta.get("source") or ""
            ),
            "content": str(chunk_meta.get("content") or ""),
            "keyword_score": round(float(row[6] or 0.0), 6),
        }


__all__ = ["RagMetadataDocumentsMixin"]
