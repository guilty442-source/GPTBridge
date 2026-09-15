"""RAG Pipeline — Canonical RAG path implementation (A371-A374).

A371: DEFAULT-PATH: source content > qdrant dense retrieval > PostgreSQL official metadata/FTS/index_state > Python domain model > typed result
A374: Binding order: 1 QDRANT_CANONICAL_RUNTIME > 2 PostgreSQL metadata/FTS/index_state > 3 Python domain model
A373: CANONICAL-TAKEOVER: normal read/write must prove Qdrant dense retrieval and PostgreSQL metadata/FTS/index_state are the live path
A374: INDEX-STATE: every indexed resource/chunk records embedding_model, embedding_dimension, chunk_size, chunk_overlap, indexed_at_utc
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import psycopg
from qdrant_client import QdrantClient
from qdrant_client.http.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue

from shared_layer.metadata_contract import (
    FIELD_CONTENT_HASH,
    FIELD_MODULE_ID,
    FIELD_RESOURCE_ID,
    FIELD_STATUS,
    FIELD_UPDATED_AT,
    FIELD_VERSION,
    STATUS_INDEXED,
)

from .rag_metadata_documents import RagMetadataDocumentsMixin
from .rag_metadata_queue import RagMetadataReconciliationMixin
from .rag_qdrant import IndexState

_logger = logging.getLogger("gptbridge.rag")

# A374: INDEX-STATE fields
INDEX_STATE_FIELDS = (
    "embedding_model",
    "embedding_dimension",
    "chunk_size",
    "chunk_overlap",
    "indexed_at_utc",
)




_logger = logging.getLogger("gptbridge.rag")


# A374 DDL — index_state follows the migration-managed shape (resource_id PK,
# indexed_at); provenance columns are added idempotently for databases that
# predate migration 009.  Chunk text stays in chunk.metadata->>'content' and
# the generated tsvector drives the canonical PostgreSQL FTS channel.
_INDEX_STATE_DDL = (
    """
    CREATE TABLE IF NOT EXISTS gptbridge_rag.index_state (
        resource_id TEXT NOT NULL PRIMARY KEY,
        module_id TEXT NOT NULL,
        embedding_model TEXT NOT NULL,
        embedding_dimension INT NOT NULL DEFAULT 0,
        chunk_size INT NOT NULL DEFAULT 0,
        chunk_overlap INT NOT NULL DEFAULT 0,
        qdrant_collection TEXT NOT NULL DEFAULT 'gptbridge_shared_knowledge',
        chunk_count INT NOT NULL DEFAULT 0,
        version BIGINT NOT NULL DEFAULT 1,
        indexed_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        content_hash TEXT NOT NULL DEFAULT '',
        qdrant_point_id TEXT NOT NULL DEFAULT '',
        postgresql_record_id TEXT,
        source_revision BIGINT NOT NULL DEFAULT 1,
        tombstone_generation INT NOT NULL DEFAULT 0,
        embedding_version INT NOT NULL DEFAULT 1,
        chunking_version INT NOT NULL DEFAULT 1,
        parser_version INT NOT NULL DEFAULT 1,
        rag_schema_version INT NOT NULL DEFAULT 1,
        pipeline_version INT NOT NULL DEFAULT 1,
        backend_generation INT NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'indexed'
    )
    """,
    """
    ALTER TABLE gptbridge_rag.index_state
        ADD COLUMN IF NOT EXISTS embedding_dimension INT NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS chunk_size INT NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS chunk_overlap INT NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS content_hash TEXT NOT NULL DEFAULT '',
        ADD COLUMN IF NOT EXISTS qdrant_point_id TEXT NOT NULL DEFAULT '',
        ADD COLUMN IF NOT EXISTS postgresql_record_id TEXT,
        ADD COLUMN IF NOT EXISTS source_revision BIGINT NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS tombstone_generation INT NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS embedding_version INT NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS chunking_version INT NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS parser_version INT NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS rag_schema_version INT NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS pipeline_version INT NOT NULL DEFAULT 1,
        ADD COLUMN IF NOT EXISTS backend_generation INT NOT NULL DEFAULT 1
    """,
    """
    ALTER TABLE gptbridge_rag.chunk
        ADD COLUMN IF NOT EXISTS content_tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('simple', coalesce(metadata ->> 'content', ''))
        ) STORED
    """,
    """
    CREATE INDEX IF NOT EXISTS rag_chunk_fts_idx
    ON gptbridge_rag.chunk USING gin (content_tsv)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_index_state_module
    ON gptbridge_rag.index_state (module_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_index_state_hash
    ON gptbridge_rag.index_state (content_hash)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_index_state_revision
    ON gptbridge_rag.index_state (module_id, resource_id, source_revision)
    """,
)

_SAGA_DDL = (
    """
    CREATE TABLE IF NOT EXISTS gptbridge_rag.reconciliation_queue (
        operation_id TEXT NOT NULL PRIMARY KEY,
        idempotency_key TEXT NOT NULL UNIQUE,
        resource_id TEXT NOT NULL,
        locator_id TEXT NOT NULL,
        source_revision BIGINT NOT NULL,
        content_hash TEXT NOT NULL,
        operation TEXT NOT NULL,
        tombstone_generation INT NOT NULL DEFAULT 0,
        embedding_model TEXT NOT NULL,
        embedding_version INT NOT NULL DEFAULT 1,
        chunk_size INT NOT NULL DEFAULT 0,
        chunk_overlap INT NOT NULL DEFAULT 0,
        chunking_version INT NOT NULL DEFAULT 1,
        parser_version INT NOT NULL DEFAULT 1,
        schema_version INT NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        attempts INT NOT NULL DEFAULT 0,
        next_retry_at TIMESTAMPTZ,
        deadline TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'pending',
        correlation_id TEXT,
        last_error TEXT,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_recon_queue_status
    ON gptbridge_rag.reconciliation_queue (status, next_retry_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_recon_queue_resource
    ON gptbridge_rag.reconciliation_queue (resource_id, source_revision)
    """,
    """
    CREATE TABLE IF NOT EXISTS gptbridge_rag.outbox_step (
        step_id TEXT NOT NULL PRIMARY KEY,
        operation_id TEXT NOT NULL,
        store TEXT NOT NULL,
        operation TEXT NOT NULL,
        succeeded BOOLEAN NOT NULL,
        applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        error TEXT,
        store_record_id TEXT
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_outbox_operation
    ON gptbridge_rag.outbox_step (operation_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS gptbridge_rag.tombstone (
        resource_id TEXT NOT NULL,
        module_id TEXT NOT NULL,
        tombstone_generation INT NOT NULL,
        source_revision BIGINT NOT NULL,
        content_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        reason TEXT NOT NULL DEFAULT 'deleted',
        purged BOOLEAN NOT NULL DEFAULT false,
        PRIMARY KEY (module_id, resource_id)
    )
    """,
)


class PostgreSQLMetadataAuthority(
    RagMetadataDocumentsMixin, RagMetadataReconciliationMixin
):
    """A374 Step 2: PostgreSQL metadata/FTS/index_state - the live authority path."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.AsyncConnection] = None
        self._healthy = False

    async def initialize(self) -> bool:
        """Initialize PostgreSQL connection."""
        try:
            self._conn = await psycopg.AsyncConnection.connect(self.dsn, autocommit=True)
            await self._ensure_schema()
            self._healthy = True
            _logger.info("PostgreSQLMetadataAuthority: initialized")
            return True
        except Exception as exc:
            _logger.warning("PostgreSQLMetadataAuthority: initialization failed: %s", exc)
            self._healthy = False
            return False

    async def _ensure_schema(self) -> None:
        """Ensure canonical tables exist (A374)."""
        if not self._conn:
            return
        await self._ensure_index_state_schema()
        await self._ensure_saga_tables()

    async def _ensure_index_state_schema(self) -> None:
        """index_state shape: migration-managed columns + A374 provenance."""
        async with self._conn.cursor() as cur:
            for statement in _INDEX_STATE_DDL:
                await cur.execute(statement)

    async def _ensure_saga_tables(self) -> None:
        """A374 durable saga stores: queue, outbox steps, tombstones."""
        async with self._conn.cursor() as cur:
            for statement in _SAGA_DDL:
                await cur.execute(statement)

    async def get_index_state(self, module_id: str, resource_id: str) -> Optional[IndexState]:
        """Fetch authoritative index state (A374)."""
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT resource_id, module_id, embedding_model, embedding_dimension,
                          chunk_size, chunk_overlap, indexed_at, content_hash,
                          qdrant_point_id, postgresql_record_id
                       FROM gptbridge_rag.index_state
                       WHERE resource_id = %s AND module_id = %s""",
                    (resource_id, module_id),
                )
                row = await cur.fetchone()
                if row:
                    return IndexState(
                        resource_id=row[0],
                        module_id=row[1],
                        embedding_model=row[2],
                        embedding_dimension=row[3],
                        chunk_size=row[4],
                        chunk_overlap=row[5],
                        indexed_at_utc=row[6],
                        content_hash=row[7],
                        qdrant_point_id=row[8],
                        postgresql_record_id=row[9],
                    )
        except Exception as exc:
            _logger.error("PostgreSQLMetadataAuthority: get_index_state failed: %s", exc)
        return None

    async def upsert_index_state(
        self,
        state: IndexState,
        *,
        collection_name: str = "gptbridge_shared_knowledge",
        chunk_count: int = 1,
    ) -> bool:
        """Upsert authoritative index state (canonical write)."""
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO gptbridge_rag.index_state
                          (resource_id, module_id, embedding_model, embedding_dimension,
                           chunk_size, chunk_overlap, indexed_at, content_hash,
                           qdrant_point_id, postgresql_record_id,
                           qdrant_collection, chunk_count, status)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'indexed')
                       ON CONFLICT (resource_id) DO UPDATE SET
                           module_id = EXCLUDED.module_id,
                           embedding_model = EXCLUDED.embedding_model,
                           embedding_dimension = EXCLUDED.embedding_dimension,
                           chunk_size = EXCLUDED.chunk_size,
                           chunk_overlap = EXCLUDED.chunk_overlap,
                           indexed_at = EXCLUDED.indexed_at,
                           content_hash = EXCLUDED.content_hash,
                           qdrant_point_id = EXCLUDED.qdrant_point_id,
                           postgresql_record_id = EXCLUDED.postgresql_record_id,
                           qdrant_collection = EXCLUDED.qdrant_collection,
                           chunk_count = EXCLUDED.chunk_count,
                           status = 'indexed',
                           updated_at = now()""",
                    (
                        state.resource_id,
                        state.module_id,
                        state.embedding_model,
                        state.embedding_dimension,
                        state.chunk_size,
                        state.chunk_overlap,
                        state.indexed_at_utc,
                        state.content_hash,
                        state.qdrant_point_id,
                        state.postgresql_record_id,
                        collection_name,
                        int(chunk_count),
                    ),
                )
            return True
        except Exception as exc:
            _logger.error("PostgreSQLMetadataAuthority: upsert_index_state failed: %s", exc)
            return False

    async def fetch_metadata(self, module_id: str, resource_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Batch-fetch metadata for resources (A207 push-down)."""
        if not self._healthy or not self._conn or not resource_ids:
            return {}
        try:
            async with self._conn.cursor() as cur:
                placeholders = ",".join(["%s"] * len(resource_ids))
                await cur.execute(
                    f"""SELECT resource_id, version, content_hash, updated_at, index_status, metadata
                       FROM gptbridge_index.resource
                       WHERE module_id = %s AND resource_id IN ({','.join(['%s'] * len(resource_ids))})""",
                    (module_id, *resource_ids),
                )
                rows = await cur.fetchall()
                return {
                    row[0]: {
                        "version": row[1],
                        "content_hash": row[2],
                        "updated_at": row[3],
                        "status": row[4],
                        "metadata": row[5] if isinstance(row[5], dict) else {},
                    }
                    for row in rows
                }
        except Exception as exc:
            _logger.error("PostgreSQLMetadataAuthority: fetch_metadata failed: %s", exc)
            return {}


    def is_healthy(self) -> bool:
        return self._healthy
