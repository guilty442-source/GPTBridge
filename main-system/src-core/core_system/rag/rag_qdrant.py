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
from qdrant_client.http.models import Distance, VectorParams, PointStruct, Filter, FieldCondition, MatchValue, MatchAny

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
    """A374 Step 1: QDRANT_CANONICAL_RUNTIME - makes healthy Qdrant the proven default dense read/write executor."""

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

    async def ensure_collection(self, dimension: Optional[int] = None) -> bool:
        """Ensure the canonical collection exists with correct vector config."""
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
        module_ids: Optional[tuple[str, ...]] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Search Qdrant for similar vectors (canonical read path)."""
        if not self._healthy or self.client is None:
            return []
        try:
            query_filter = None
            if module_ids:
                query_filter = Filter(
                    must=[FieldCondition(key="module_id", match=MatchAny(any=list(module_ids)))]
                )
            elif module_id:
                query_filter = Filter(
                    must=[FieldCondition(key="module_id", match=MatchValue(value=module_id))]
                )
            response = self.client.query_points(
                collection_name=self.config.collection_name,
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
                }
                for hit in response.points
            ]
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: search failed: %s", exc)
            return []

    async def delete_resource(self, module_id: str, resource_id: str) -> bool:
        """Delete all points for a resource (tombstone/archive reconcile)."""
        if not self._healthy or self.client is None:
            return False
        try:
            self.client.delete(
                collection_name=self.config.collection_name,
                points_selector=Filter(
                    must=[
                        FieldCondition(
                            key="module_id", match=MatchValue(value=module_id)
                        ),
                        FieldCondition(
                            key="document_resource_id",
                            match=MatchValue(value=resource_id),
                        ),
                    ]
                ),
                wait=True,
            )
            return True
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: delete failed: %s", exc)
            return False

    def points_count(self) -> Optional[int]:
        """Current point count in the canonical collection (None when unavailable)."""
        if not self._healthy or self.client is None:
            return None
        try:
            info = self.client.get_collection(self.config.collection_name)
            return int(info.points_count or 0)
        except Exception as exc:
            _logger.warning("QdrantCanonicalRuntime: points_count failed: %s", exc)
            return None

    def is_healthy(self) -> bool:
        return self._healthy
