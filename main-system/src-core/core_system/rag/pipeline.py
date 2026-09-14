"""RAG Pipeline — Canonical RAG path implementation (A371-A374).

A371: DEFAULT-PATH: source content > qdrant dense retrieval > PostgreSQL official metadata/FTS/index_state > Python domain model > typed result
A372: Binding order: 1 QDRANT_CANONICAL_RUNTIME > 2 PostgreSQL metadata/FTS/index_state > 3 Python domain model
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


@dataclass(frozen=True)
class IndexState:
    """A374: Authoritative index state for each indexed resource/chunk."""
    resource_id: str
    module_id: str
    embedding_model: str
    embedding_dimension: int
    chunk_size: int
    chunk_overlap: int
    indexed_at_utc: str
    content_hash: str
    qdrant_point_id: str
    postgresql_record_id: Optional[str] = None


@dataclass(frozen=True)
class RagQueryResult:
    """Result from the canonical RAG path."""
    resource_id: str
    module_id: str
    content: str
    score: float
    metadata: dict[str, Any]
    index_state: IndexState


@dataclass
class RagPipelineConfig:
    """Configuration for the canonical RAG pipeline."""
    qdrant_url: str
    qdrant_api_key: Optional[str]
    collection_name: str
    postgresql_dsn: str
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536
    chunk_size: int = 512
    chunk_overlap: int = 64
    top_k: int = 10
    score_threshold: float = 0.7


class QdrantCanonicalRuntime:
    """A372 Step 1: QDRANT_CANONICAL_RUNTIME - makes healthy Qdrant the proven default dense read/write executor."""

    def __init__(self, config: RagPipelineConfig) -> None:
        self.config = config
        self.client: Optional[QdrantClient] = None
        self._healthy = False

    async def initialize(self) -> bool:
        """Initialize Qdrant connection and ensure collection exists."""
        try:
            self.client = QdrantClient(
                url=self.config.qdrant_url,
                api_key=self.config.qdrant_api_key,
                timeout=30,
            )
            # Check health
            collections = self.client.get_collections()
            self._healthy = True
            _logger.info("QdrantCanonicalRuntime: healthy, collections=%d", len(collections.collections))
            return True
        except Exception as exc:
            _logger.warning("QdrantCanonicalRuntime: initialization failed: %s", exc)
            self._healthy = False
            return False

    async def ensure_collection(self) -> bool:
        """Ensure the canonical collection exists with correct vector config."""
        if not self._healthy or self.client is None:
            return False
        try:
            collections = self.client.get_collections()
            names = {c.name for c in collections.collections}
            if self.config.collection_name not in names:
                self.client.create_collection(
                    collection_name=self.config.collection_name,
                    vectors_config=VectorParams(
                        size=self.config.embedding_dimension,
                        distance=Distance.COSINE,
                    ),
                )
                _logger.info("QdrantCanonicalRuntime: created collection %s", self.config.collection_name)
            return True
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: ensure_collection failed: %s", exc)
            return False

    async def upsert_points(self, points: list[PointStruct]) -> bool:
        """Upsert vectors to Qdrant (canonical write path)."""
        if not self._healthy or self.client is None:
            return False
        try:
            self.client.upsert(
                collection_name=self.config.collection_name,
                points=points,
                wait=True,
            )
            return True
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: upsert failed: %s", exc)
            return False

    async def search(
        self,
        query_vector: list[float],
        module_id: Optional[str] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Search Qdrant for similar vectors (canonical read path)."""
        if not self._healthy or self.client is None:
            return []
        try:
            query_filter = None
            if module_id:
                query_filter = Filter(
                    must=[FieldCondition(key="module_id", match=MatchValue(value=module_id))]
                )
            results = self.client.search(
                collection_name=self.config.collection_name,
                query_vector=query_vector,
                query_filter=query_filter,
                limit=top_k or self.config.top_k,
                score_threshold=score_threshold or self.config.score_threshold,
                with_payload=True,
                with_vectors=False,
            )
            return [
                {
                    "id": hit.id,
                    "score": hit.score,
                    "payload": hit.payload,
                }
                for hit in results
            ]
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: search failed: %s", exc)
            return []

    def is_healthy(self) -> bool:
        return self._healthy


class PostgreSQLMetadataAuthority:
    """A372 Step 2: PostgreSQL metadata/FTS/index_state - the live authority path."""

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


class PythonDomainModel:
    """A372 Step 3: Python domain model - sole production owner of typed results."""

    def __init__(self, config: RagPipelineConfig) -> None:
        self.config = config

    def build_typed_result(
        self,
        qdrant_hits: list[dict[str, Any]],
        pg_metadata: dict[str, dict[str, Any]],
        index_states: dict[str, IndexState],
    ) -> list[RagQueryResult]:
        """Build typed domain results from canonical sources."""
        results = []
        for hit in qdrant_hits:
            payload = hit.get("payload", {})
            resource_id = payload.get("resource_id") or hit.get("id")
            module_id = payload.get("module_id")

            if not resource_id or not module_id:
                continue

            key = f"{module_id}:{resource_id}"
            index_state = index_states.get(key)
            pg_meta = pg_metadata.get(key, {})

            if not index_state:
                _logger.warning("PythonDomainModel: missing index_state for %s", key)
                continue

            results.append(RagQueryResult(
                resource_id=resource_id,
                module_id=module_id,
                content=payload.get("content", ""),
                score=hit.get("score", 0.0),
                metadata={**payload, **pg_meta.get("metadata", {})},
                index_state=index_state,
            ))
        return results


class CanonicalRagPipeline:
    """A371-A374: Canonical RAG pipeline implementation.

    DEFAULT-PATH: source content > qdrant dense retrieval > PostgreSQL official metadata/FTS/index_state > Python domain model > typed result

    Binding order (A372):
    1. QDRANT_CANONICAL_RUNTIME (Qdrant dense retrieval)
    2. PostgreSQL metadata/FTS/index_state (live authority)
    3. Python domain model (sole production owner)

    CANONICAL-TAKEOVER (A373): normal execution must prove Qdrant + PostgreSQL are live path.
    """

    def __init__(self, config: RagPipelineConfig) -> None:
        self.config = config
        self.qdrant = QdrantCanonicalRuntime(config)
        self.postgresql = PostgreSQLMetadataAuthority(config.postgresql_dsn)
        self.domain_model = PythonDomainModel(config)
        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize all canonical components in order (A372)."""
        _logger.info("CanonicalRagPipeline: initializing...")

        # Step 1: Qdrant canonical runtime
        qdrant_ok = await self.qdrant.initialize()
        if qdrant_ok:
            await self.qdrant.ensure_collection()

        # Step 2: PostgreSQL metadata authority
        pg_ok = await self.postgresql.initialize()

        self._initialized = qdrant_ok and pg_ok
        _logger.info("CanonicalRagPipeline: initialized=%s (qdrant=%s, pg=%s)", self._initialized, qdrant_ok, pg_ok)
        return self._initialized

    def is_ready(self) -> bool:
        """A373: Prove Qdrant + PostgreSQL are live path."""
        return self._initialized and self.qdrant.is_healthy() and self.postgresql.is_healthy()

    async def index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> IndexState:
        """Index a resource through the canonical path (write path)."""
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        point_id = str(uuid.uuid4())
        now_utc = datetime.now(timezone.utc).isoformat()

        # Step 1: Write to Qdrant (canonical write)
        point = PointStruct(
            id=point_id,
            vector=embedding,
            payload={
                "resource_id": resource_id,
                "module_id": module_id,
                "content": content,
                "content_hash": content_hash,
                "indexed_at_utc": now_utc,
                **metadata,
            },
        )
        await self.qdrant.upsert_points([point])

        # Step 2: Record index state in PostgreSQL (authoritative)
        index_state = IndexState(
            resource_id=resource_id,
            module_id=module_id,
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            indexed_at_utc=now_utc,
            content_hash=content_hash,
            qdrant_point_id=point_id,
        )
        await self.postgresql.upsert_index_state(index_state)

        return index_state

    async def query(
        self,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> list[RagQueryResult]:
        """Query through the canonical RAG path (read path)."""
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")

        # A373: CANONICAL-TAKEOVER - prove Qdrant + PostgreSQL are live
        if not self.qdrant.is_healthy():
            raise RuntimeError("Qdrant canonical runtime not healthy")
        if not self.postgresql.is_healthy():
            raise RuntimeError("PostgreSQL metadata authority not healthy")

        # Step 1: Qdrant dense retrieval
        qdrant_hits = await self.qdrant.search(
            query_vector=query_embedding,
            module_id=module_id,
            top_k=top_k,
            score_threshold=score_threshold,
        )

        if not qdrant_hits:
            return []

        # Step 2: Batch-fetch PostgreSQL metadata (A207 push-down)
        resource_ids = [
            hit.get("payload", {}).get("resource_id") or hit.get("id")
            for hit in qdrant_hits
        ]
        module_ids = set(
            hit.get("payload", {}).get("module_id")
            for hit in qdrant_hits
            if hit.get("payload", {}).get("module_id")
        )

        pg_metadata = {}
        index_states = {}

        for mid in module_ids:
            pg_metadata.update(await self.postgresql.fetch_metadata(mid, resource_ids))

        # Step 3: Fetch index states
        for mid in module_ids:
            for rid in resource_ids:
                state = await self.postgresql.get_index_state(mid, rid)
                if state:
                    index_states[f"{mid}:{rid}"] = state

        # Step 4: Build typed results via Python domain model
        return self.domain_model.build_typed_result(qdrant_hits, pg_metadata, index_states)

    async def get_index_state(self, module_id: str, resource_id: str) -> Optional[IndexState]:
        """Get authoritative index state (A374)."""
        return await self.postgresql.get_index_state(module_id, resource_id)

    async def health_check(self) -> dict[str, Any]:
        """Health check for all components."""
        return {
            "pipeline_ready": self.is_ready(),
            "qdrant": {
                "healthy": self.qdrant.is_healthy(),
                "collection": self.config.collection_name,
            },
            "postgresql": {
                "healthy": self.postgresql.is_healthy(),
            },
            "domain_model": "ok",
        }


def create_rag_pipeline_from_env() -> CanonicalRagPipeline:
    """Create pipeline from environment variables."""
    config = RagPipelineConfig(
        qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        qdrant_api_key=os.environ.get("QDRANT_API_KEY"),
        collection_name=os.environ.get("QDRANT_COLLECTION", "gptbridge_rag"),
        postgresql_dsn=os.environ.get("POSTGRESQL_DSN", ""),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small"),
        embedding_dimension=int(os.environ.get("EMBEDDING_DIMENSION", "1536")),
        chunk_size=int(os.environ.get("CHUNK_SIZE", "512")),
        chunk_overlap=int(os.environ.get("CHUNK_OVERLAP", "64")),
    )
    return CanonicalRagPipeline(config)


__all__ = [
    "CanonicalRagPipeline",
    "RagPipelineConfig",
    "IndexState",
    "RagQueryResult",
    "QdrantCanonicalRuntime",
    "PostgreSQLMetadataAuthority",
    "PythonDomainModel",
    "create_rag_pipeline_from_env",
]