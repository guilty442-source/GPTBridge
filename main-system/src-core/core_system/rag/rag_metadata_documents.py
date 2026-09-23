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
                if chunks:
                    await cur.executemany(
                        _CHUNK_INSERT_SQL,
                        [
                            _chunk_params(chunk, resource_id, module_id, embedding_model)
                            for chunk in chunks
                        ],
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

    async def fetch_resource_by_locator(
        self, module_id: str, locator_id: str
    ) -> Optional[dict[str, Any]]:
        """Resolve an opaque ``locator_id`` to the canonical resource
        row.  Used by module-agnostic content resolvers — the owning
        module hands RAG a locator, never a physical path."""
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT resource_id, resource_type, content_hash,
                              version, metadata
                       FROM gptbridge_index.resource
                       WHERE module_id = %s AND locator_id = %s""",
                    (module_id, locator_id),
                )
                row = await cur.fetchone()
            if row is None:
                return None
            metadata = row[4] if isinstance(row[4], dict) else {}
            return {
                "resource_id": str(row[0]),
                "resource_type": str(row[1] or ""),
                "content_hash": str(row[2] or ""),
                "version": int(row[3] or 1),
                "metadata": metadata,
            }
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: fetch_resource_by_locator "
                "failed: %s",
                exc,
            )
            return None

    async def fetch_resource_chunks(
        self, module_id: str, resource_id: str, *, max_chunks: int = 16384
    ) -> list[dict[str, Any]]:
        """Fetch a resource's chunk rows with content for outbox replay.

        Content lives in ``chunk.metadata->>'content'`` (PostgreSQL is the
        content authority; Qdrant payloads never carry it).

        Bounded read (P15): fetches at most ``max_chunks + 1`` rows and
        raises ``RuntimeError`` when the resource exceeds the cap — callers
        rebuild/replay/resolve *complete* content, so a silently truncated
        chunk set must never reach them.
        """
        if not self._healthy or not self._conn:
            return []
        cap = max(1, int(max_chunks))
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT chunk_id, qdrant_point_id::text, sequence,
                              character_start, character_end, metadata
                       FROM gptbridge_rag.chunk
                       WHERE module_id = %s AND resource_id = %s
                       ORDER BY sequence
                       LIMIT %s""",
                    (module_id, resource_id, cap + 1),
                )
                rows = await cur.fetchall()
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: fetch_resource_chunks failed: %s",
                exc,
            )
            return []
        if len(rows) > cap:
            raise RuntimeError(
                f"RESOURCE_CHUNK_CAP_EXCEEDED: {module_id}:{resource_id} "
                f"exceeds {cap} chunks — refusing to hand callers a "
                f"silently truncated content set"
            )
        out: list[dict[str, Any]] = []
        for row in rows:
            meta = row[5] if isinstance(row[5], dict) else {}
            out.append({
                "chunk_id": str(row[0]),
                "qdrant_point_id": str(row[1]) if row[1] else None,
                "sequence": int(row[2]),
                "character_start": int(row[3]),
                "character_end": int(row[4]),
                "content": str(meta.get("content") or ""),
                "payload": {
                    "content_hash": str(meta.get("content_hash") or ""),
                },
            })
        return out

    async def fetch_chunks_for_points(
        self,
        module_ids: tuple[str, ...],
        point_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        """Canonical read barrier + content hydration for Qdrant hits.

        Returns chunk records keyed by ``qdrant_point_id`` (text).  Only
        chunks whose resource is not tombstoned/deleted and that carry no
        authoritative tombstone row are returned — a hit missing from this
        map can never enter the evidence pool.
        """
        if not self._healthy or not self._conn or not module_ids or not point_ids:
            return {}
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT chunk.qdrant_point_id::text, chunk.chunk_id,
                              chunk.resource_id, chunk.module_id, chunk.sequence,
                              chunk.character_start, chunk.character_end,
                              chunk.metadata AS chunk_metadata,
                              resource.metadata AS resource_metadata,
                              resource.index_status
                       FROM gptbridge_rag.chunk AS chunk
                       JOIN gptbridge_index.resource AS resource
                         ON resource.resource_id = chunk.resource_id
                       WHERE chunk.module_id = ANY(%s)
                         AND chunk.qdrant_point_id::text = ANY(%s)
                         AND resource.index_status NOT IN
                             ('tombstoned', 'deleted', 'purged')
                         AND NOT EXISTS (
                             SELECT 1 FROM gptbridge_rag.tombstone AS t
                             WHERE t.module_id = chunk.module_id
                               AND t.resource_id = chunk.resource_id
                               AND t.purged = false
                         )""",
                    (list(module_ids), [str(p) for p in point_ids]),
                )
                rows = await cur.fetchall()
            return {
                str(row[0]): self._chunk_barrier_row(row) for row in rows if row[0]
            }
        except Exception as exc:
            _logger.error(
                "PostgreSQLMetadataAuthority: fetch_chunks_for_points failed: %s",
                exc,
            )
            return {}

    @staticmethod
    def _chunk_barrier_row(row: Any) -> dict[str, Any]:
        chunk_meta = row[7] if isinstance(row[7], dict) else {}
        resource_meta = row[8] if isinstance(row[8], dict) else {}
        return {
            "point_id": str(row[0]),
            "chunk_id": str(row[1]),
            "resource_id": str(row[2]),
            "document_resource_id": str(row[2]),
            "document_id": str(resource_meta.get("document_id") or ""),
            "module_id": str(row[3]),
            "sequence": int(row[4]),
            "character_start": int(row[5]),
            "character_end": int(row[6]),
            "title": str(
                chunk_meta.get("title") or resource_meta.get("title") or ""
            ),
            "source": str(
                resource_meta.get("source") or chunk_meta.get("source") or ""
            ),
            "content": str(chunk_meta.get("content") or ""),
            "resource_status": str(row[9] or ""),
        }

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
                await cur.execute(  # sql-ok: code-controlled SQL composition
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
