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


class PostgreSQLMetadataAuthority:
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
        """Ensure canonical tables exist."""
        if not self._conn:
            return
        async with self._conn.cursor() as cur:
            # Canonical index_state table (A374)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS gptbridge_rag.index_state (
                    resource_id TEXT NOT NULL,
                    module_id TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    embedding_dimension INT NOT NULL,
                    chunk_size INT NOT NULL,
                    chunk_overlap INT NOT NULL,
                    indexed_at_utc TIMESTAMPTZ NOT NULL,
                    content_hash TEXT NOT NULL,
                    qdrant_point_id TEXT NOT NULL,
                    postgresql_record_id TEXT,
                    PRIMARY KEY (module_id, resource_id)
                )
            """)
            await cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_index_state_module
                ON gptbridge_rag.index_state (module_id)
            """)
            await cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_index_state_hash
                ON gptbridge_rag.index_state (content_hash)
            """)

    async def get_index_state(self, module_id: str, resource_id: str) -> Optional[IndexState]:
        """Fetch authoritative index state (A374)."""
        if not self._healthy or not self._conn:
            return None
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """SELECT resource_id, module_id, embedding_model, embedding_dimension,
                          chunk_size, chunk_overlap, indexed_at_utc, content_hash,
                          qdrant_point_id, postgresql_record_id
                       FROM gptbridge_rag.index_state
                       WHERE module_id = %s AND resource_id = %s""",
                    (module_id, resource_id),
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

    async def upsert_index_state(self, state: IndexState) -> bool:
        """Upsert authoritative index state (canonical write)."""
        if not self._healthy or not self._conn:
            return False
        try:
            async with self._conn.cursor() as cur:
                await cur.execute(
                    """INSERT INTO gptbridge_rag.index_state
                          (resource_id, module_id, embedding_model, embedding_dimension,
                           chunk_size, chunk_overlap, indexed_at_utc, content_hash,
                           qdrant_point_id, postgresql_record_id)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (module_id, resource_id) DO UPDATE SET
                           embedding_model = EXCLUDED.embedding_model,
                           embedding_dimension = EXCLUDED.embedding_dimension,
                           chunk_size = EXCLUDED.chunk_size,
                           chunk_overlap = EXCLUDED.chunk_overlap,
                           indexed_at_utc = EXCLUDED.indexed_at_utc,
                           content_hash = EXCLUDED.content_hash,
                           qdrant_point_id = EXCLUDED.qdrant_point_id,
                           postgresql_record_id = EXCLUDED.postgresql_record_id""",
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
                    f"""SELECT resource_id, version, content_hash, updated_at, status, metadata_json
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
                        "metadata": json.loads(row[5]) if row[5] else {},
                    }
                    for row in rows
                }
        except Exception as exc:
            _logger.error("PostgreSQLMetadataAuthority: fetch_metadata failed: %s", exc)
            return {}

    def is_healthy(self) -> bool:
        return self._healthy
