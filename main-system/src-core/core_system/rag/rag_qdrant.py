"""RAG Pipeline — Canonical RAG path implementation (A371-A374).

A371: DEFAULT-PATH: source content > qdrant dense retrieval > PostgreSQL official metadata/FTS/index_state > Python domain model > typed result
A374: Binding order: 1 QDRANT_CANONICAL_RUNTIME > 2 PostgreSQL metadata/FTS/index_state > 3 Python domain model
A373: CANONICAL-TAKEOVER: normal read/write must prove Qdrant dense retrieval and PostgreSQL metadata/FTS/index_state are the live path
A374: INDEX-STATE: every indexed resource/chunk records embedding_model, embedding_dimension, chunk_size, chunk_overlap, indexed_at_utc

A486+A487: Index Generation + Alias switching. Queries target logical alias; physical collections are versioned.
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
from qdrant_client.http.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue, MatchAny, PayloadSchemaType

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
    generation_id: Optional[str] = None  # A486: bind to index generation


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
    # Governed local embedding contract (A49): qwen3-embedding:4b via the
    # local Ollama runtime, 2560 dimensions — matches the canonical
    # gptbridge_shared_knowledge collection.  No remote embedding provider
    # may be the default canonical path.
    embedding_model: str = "qwen3-embedding:4b"
    embedding_dimension: int = 2560
    embedding_provider: str = "ollama"
    chunk_size: int = 1200
    chunk_overlap: int = 200
    top_k: int = 10
    score_threshold: float = 0.0
    # A374 durability: pending_rag_mutation queue + degraded stores must
    # survive restarts; None keeps the in-memory/temp fallbacks for tests.
    queue_db_path: Optional[str] = None
    degraded_root: Optional[str] = None


class QdrantCanonicalRuntime:
    """A374 Step 1: QDRANT_CANONICAL_RUNTIME - makes healthy Qdrant the proven default dense read/write executor.

    A486: Supports alias-based queries. The canonical alias (e.g., gptbridge_shared_knowledge)
    always points to the ACTIVE generation collection. Physical collections are versioned
    (e.g., gptbridge_shared_knowledge_gen-20260916-001).
    """

    def __init__(self, config: RagPipelineConfig) -> None:
        self.config = config
        self.client: Optional[QdrantClient] = None
        self._healthy = False
        self._alias_name = config.collection_name  # The logical alias name

    async def initialize(self) -> bool:
        """Initialize Qdrant connection and ensure alias target collection exists."""
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

    async def ensure_collection(self, dimension: Optional[int] = None) -> bool:
        """Ensure the canonical collection (alias target) exists with correct vector config."""
        if not self._healthy or self.client is None:
            return False
        size = int(dimension or self.config.embedding_dimension or 0)
        try:
            collections = self.client.get_collections()
            names = {c.name for c in collections.collections}
            if self.config.collection_name not in names:
                if size <= 0:
                    return False
                self.client.create_collection(
                    collection_name=self.config.collection_name,
                    vectors_config=VectorParams(
                        size=size,
                        distance=Distance.COSINE,
                    ),
                )
                _logger.info("QdrantCanonicalRuntime: created collection %s", self.config.collection_name)
            return True
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: ensure_collection failed: %s", exc)
            return False

    async def ensure_payload_indexes(self) -> bool:
        """Create payload indexes for filtered queries (A486: module_id, classification, generation_id, etc.)."""
        if not self._healthy or self.client is None:
            return False
        try:
            indexes = [
                ("module_id", PayloadSchemaType.KEYWORD),
                ("classification", PayloadSchemaType.KEYWORD),
                ("resource_type", PayloadSchemaType.KEYWORD),
                ("data_category", PayloadSchemaType.KEYWORD),
                ("rag_type", PayloadSchemaType.KEYWORD),
                ("generation_id", PayloadSchemaType.KEYWORD),
                ("version", PayloadSchemaType.INTEGER),
            ]
            for field_name, schema_type in indexes:
                try:
                    self.client.create_payload_index(
                        collection_name=self.config.collection_name,
                        field_name=field_name,
                        field_schema=schema_type,
                    )
                    _logger.info("QdrantCanonicalRuntime: created payload index for %s", field_name)
                except Exception:
                    # Index may already exist
                    pass
            return True
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: ensure_payload_indexes failed: %s", exc)
            return False

    def _get_target_collection(self, generation_id: Optional[str] = None) -> str:
        """Resolve target collection: alias for ACTIVE, specific generation for BUILDING/VERIFYING."""
        if generation_id:
            return f"{self.config.collection_name}_{generation_id}"
        return self.config.collection_name  # Query against alias

    async def upsert_points(
        self,
        points: list[PointStruct],
        generation_id: Optional[str] = None,
    ) -> bool:
        """Upsert vectors to Qdrant (canonical write path).

        If generation_id provided, writes to that physical collection.
        Otherwise writes to the alias (ACTIVE generation).
        """
        if not self._healthy or self.client is None:
            return False
        target = self._get_target_collection(generation_id)
        try:
            self.client.upsert(
                collection_name=target,
                points=points,
                wait=True,
            )
            return True
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: upsert to %s failed: %s", target, exc)
            return False

    async def search(
        self,
        query_vector: list[float],
        module_id: Optional[str] = None,
        module_ids: Optional[tuple[str, ...]] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
        generation_id: Optional[str] = None,
        additional_filter: Optional[Filter] = None,
    ) -> list[dict[str, Any]]:
        """Search Qdrant for similar vectors (canonical read path).

        Queries the alias by default (ACTIVE generation). Pass generation_id
        to query a specific physical collection.
        """
        if not self._healthy or self.client is None:
            return []
        target = self._get_target_collection(generation_id)
        try:
            must_conditions = []
            if module_ids:
                must_conditions.append(FieldCondition(key="module_id", match=MatchAny(any=list(module_ids))))
            elif module_id:
                must_conditions.append(FieldCondition(key="module_id", match=MatchValue(value=module_id)))

            if additional_filter:
                # Merge additional filter conditions
                if hasattr(additional_filter, 'must'):
                    must_conditions.extend(additional_filter.must)

            query_filter = Filter(must=must_conditions) if must_conditions else None

            response = self.client.query_points(
                collection_name=target,
                query=query_vector,
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
                    "point_id": hit.id,
                }
                for hit in response.points
            ]
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: search on %s failed: %s", target, exc)
            return []

    async def search_with_payload_filter(
        self,
        query_vector: list[float],
        payload_filter: Filter,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
        generation_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Search with arbitrary payload filter (for hybrid/reranker pre-filtering)."""
        return await self.search(
            query_vector=query_vector,
            top_k=top_k,
            score_threshold=score_threshold,
            generation_id=generation_id,
            additional_filter=payload_filter,
        )

    async def delete_resource(
        self,
        module_id: str,
        resource_id: str,
        generation_id: Optional[str] = None,
    ) -> bool:
        """Delete all points for a resource (tombstone/archive reconcile)."""
        if not self._healthy or self.client is None:
            return False
        target = self._get_target_collection(generation_id)
        try:
            self.client.delete(
                collection_name=target,
                points_selector=Filter(
                    must=[
                        FieldCondition(key="module_id", match=MatchValue(value=module_id)),
                        FieldCondition(key="document_resource_id", match=MatchValue(value=resource_id)),
                    ]
                ),
                wait=True,
            )
            return True
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: delete on %s failed: %s", target, exc)
            return False

    def points_count(self, generation_id: Optional[str] = None) -> Optional[int]:
        """Current point count in the target collection (None when unavailable)."""
        if not self._healthy or self.client is None:
            return None
        target = self._get_target_collection(generation_id)
        try:
            info = self.client.get_collection(target)
            return int(info.points_count or 0)
        except Exception as exc:
            _logger.warning("QdrantCanonicalRuntime: points_count on %s failed: %s", target, exc)
            return None

    def is_healthy(self) -> bool:
        return self._healthy

    async def create_alias(self, alias_name: str, collection_name: str) -> bool:
        """Atomically create/update alias to point to collection (A486)."""
        if not self._healthy or self.client is None:
            return False
        try:
            # Qdrant create_alias will replace existing alias
            self.client.create_alias(
                alias_name=alias_name,
                collection_name=collection_name,
            )
            _logger.info("QdrantCanonicalRuntime: alias %s -> %s", alias_name, collection_name)
            return True
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: create_alias failed: %s", exc)
            return False

    async def get_alias_target(self, alias_name: str) -> Optional[str]:
        """Get the collection currently pointed to by alias."""
        if not self._healthy or self.client is None:
            return None
        try:
            aliases = self.client.get_aliases()
            for alias in aliases.aliases:
                if alias.alias_name == alias_name:
                    return alias.collection_name
            return None
        except Exception as exc:
            _logger.warning("QdrantCanonicalRuntime: get_alias_target failed: %s", exc)
            return None
