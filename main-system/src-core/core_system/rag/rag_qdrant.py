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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, AsyncIterator, Iterable, Optional

from urllib.parse import urlparse

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
from shared_layer.security.qdrant_scope import (
    QdrantScopeError,
    assert_payload_scoped,
    require_scope,
)

_logger = logging.getLogger("gptbridge.rag")


def _observe_qdrant_latency(latency_ms: float) -> None:
    """P4 adaptive plane 生產者：canonical 檢索延遲 → ``qdrant_latency_ms``。

    欄位級合併、失敗靜默——量測只是提示，不得影響檢索主流程。
    """
    try:
        from shared_layer.adaptive import LoadSignals, get_plane

        get_plane().observe_merge(
            LoadSignals(qdrant_latency_ms=latency_ms),
            fields=("qdrant_latency_ms",),
        )
    except Exception:
        pass

# A374: INDEX-STATE fields
INDEX_STATE_FIELDS = (
    "embedding_model",
    "embedding_dimension",
    "chunk_size",
    "chunk_overlap",
    "indexed_at_utc",
)

# Canonical takeover (RAG-01..07): Qdrant is the dense vector authority only.
# Payloads may carry opaque ids + filterable metadata — never content or
# physical locators; PostgreSQL owns content/FTS/locators.
FORBIDDEN_PAYLOAD_FIELDS = frozenset(
    {"content", "text", "path", "physical_location", "windows_path", "source"}
)
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def is_loopback_url(url: str) -> bool:
    """RAG rule 2: Qdrant is local-owned, local-only — loopback hosts only."""
    host = (urlparse(str(url)).hostname or "").strip().lower()
    return host in _LOOPBACK_HOSTS


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strip fields that must never enter the Qdrant canonical payload.

    Removes the five named forbidden fields plus ``source`` (a physical
    path) and any key that itself denotes a path/location — deterministic,
    no silent pass-through of caller-supplied keys.
    """
    clean: dict[str, Any] = {}
    for key, value in (payload or {}).items():
        lowered = str(key).lower()
        if lowered in FORBIDDEN_PAYLOAD_FIELDS:
            continue
        if "path" in lowered or "location" in lowered:
            continue
        clean[str(key)] = value
    return clean


def _collection_vector_size(info: Any) -> Optional[int]:
    """Read the configured vector size off a Qdrant collection info object."""
    try:
        vectors = info.config.params.vectors
    except AttributeError:
        return None
    size = getattr(vectors, "size", None)
    if size is not None:
        return int(size)
    if isinstance(vectors, dict) and vectors:
        first = next(iter(vectors.values()))
        inner = getattr(first, "size", None)
        return int(inner) if inner is not None else None
    return None


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
    status: str = "indexed"  # read barrier: only 'indexed'/'active' may serve


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
        # Canonical takeover: sticky contract violations surface as BLOCKED.
        self.collection_error: Optional[str] = None
        self.last_error: Optional[str] = None

    async def initialize(self) -> bool:
        """Initialize Qdrant connection and ensure alias target collection exists."""
        if not is_loopback_url(self.config.qdrant_url):
            self.last_error = (
                f"QDRANT_URL_NOT_LOOPBACK: {self.config.qdrant_url} — the "
                "canonical vector index is local-owned and loopback-only"
            )
            _logger.error("QdrantCanonicalRuntime: %s", self.last_error)
            self._healthy = False
            return False
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
            self.last_error = str(exc)
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
            # Existing collection: verify the vector contract; a dimension
            # mismatch is a hard INDEX_MISMATCH — never overwrite or silently
            # fall back onto an incompatible collection.
            existing_size = _collection_vector_size(
                self.client.get_collection(self.config.collection_name)
            )
            if size > 0 and existing_size is not None and existing_size != size:
                self.collection_error = (
                    f"INDEX_MISMATCH:collection={self.config.collection_name} "
                    f"dimension={existing_size} expected={size}"
                )
                _logger.error("QdrantCanonicalRuntime: %s", self.collection_error)
                return False
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

        Every point payload is sanitized (no content/physical locators may
        enter the canonical index, A371) and must carry the mandatory
        ``module_id`` scope field (A52 qdrant-scope, fail closed).

        If generation_id provided, writes to that physical collection.
        Otherwise writes to the alias (ACTIVE generation).
        """
        if not self._healthy or self.client is None:
            return False
        target = self._get_target_collection(generation_id)
        validated: list[Any] = []
        for point in points:
            if isinstance(point, dict):
                payload = dict(point.get("payload") or {})
                cleaned = sanitize_payload(payload)
                assert_payload_scoped(cleaned)
                validated.append({**point, "payload": cleaned})
            else:
                payload = dict(getattr(point, "payload", None) or {})
                cleaned = sanitize_payload(payload)
                assert_payload_scoped(cleaned)
                validated.append(
                    PointStruct(id=point.id, vector=point.vector, payload=cleaned)
                )
        try:
            self.client.upsert(
                collection_name=target,
                points=validated,
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

        ``module_id``/``module_ids`` scope is mandatory: a search without a
        non-empty module scope raises ``QdrantScopeError`` (fail closed) and
        can never turn into a whole-collection scan.  The scope filter is
        always applied, merged with any additional filter conditions.

        Queries the alias by default (ACTIVE generation). Pass generation_id
        to query a specific physical collection.
        """
        scope_modules = [str(m).strip() for m in (module_ids or ()) if str(m).strip()]
        if not scope_modules and module_id:
            scope_modules = [str(module_id)]
        scope = require_scope(scope_modules)
        if not self._healthy or self.client is None:
            return []
        target = self._get_target_collection(generation_id)
        try:
            must_conditions: list[Any] = [
                FieldCondition(
                    key="module_id", match=MatchAny(any=list(scope.module_ids))
                )
            ]
            if additional_filter is not None and getattr(
                additional_filter, "must", None
            ):
                must_conditions.extend(additional_filter.must)

            query_filter = Filter(must=must_conditions)

            qdrant_start = time.monotonic()
            response = self.client.query_points(
                collection_name=target,
                query=query_vector,
                query_filter=query_filter,
                limit=top_k or self.config.top_k,
                score_threshold=score_threshold or self.config.score_threshold,
                with_payload=True,
                with_vectors=False,
            )
            _observe_qdrant_latency((time.monotonic() - qdrant_start) * 1000.0)
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
        *,
        module_id: str = "",
        module_ids: Optional[tuple[str, ...]] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
        generation_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """Search with an additional payload filter (hybrid/reranker pre-filter).

        The module scope remains mandatory and is applied by :meth:`search`.
        """
        return await self.search(
            query_vector=query_vector,
            module_id=module_id or None,
            module_ids=module_ids,
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
        """Delete all points for a resource (tombstone/archive reconcile).

        A non-empty module scope is mandatory.  The filter matches both the
        canonical ``resource_id`` and the legacy ``document_resource_id``
        payload key so a producer/consumer key drift cannot silently leave
        orphan vectors; deletion is verified by a follow-up count and only
        reported successful when zero points remain.
        """
        scope = require_scope([module_id])
        if not self._healthy or self.client is None:
            return False
        target = self._get_target_collection(generation_id)
        resource_selector = Filter(
            must=[
                FieldCondition(
                    key="module_id", match=MatchValue(value=scope.module_ids[0])
                )
            ],
            should=[
                FieldCondition(key="resource_id", match=MatchValue(value=resource_id)),
                FieldCondition(
                    key="document_resource_id", match=MatchValue(value=resource_id)
                ),
            ],
        )
        try:
            self.client.delete(
                collection_name=target,
                points_selector=resource_selector,
                wait=True,
            )
            remaining = self.client.count(
                collection_name=target,
                count_filter=resource_selector,
                exact=True,
            )
            count = int(getattr(remaining, "count", 0) or 0)
            if count:
                _logger.error(
                    "QdrantCanonicalRuntime: %d points remain after delete on %s "
                    "(module=%s resource=%s)",
                    count,
                    target,
                    scope.module_ids[0],
                    resource_id,
                )
                return False
            return True
        except Exception as exc:
            _logger.error("QdrantCanonicalRuntime: delete on %s failed: %s", target, exc)
            return False

    def count_resource_points(
        self,
        module_id: str,
        resource_id: str,
        generation_id: Optional[str] = None,
    ) -> Optional[int]:
        """Exact per-resource point count (§10.6 parity sweep).

        Matches the same module scope + resource_id/document_resource_id
        selector shape as ``delete_resource`` so drift detection and repair
        agree on which points belong to a resource.
        """
        scope = require_scope([module_id])
        if not self._healthy or self.client is None:
            return None
        target = self._get_target_collection(generation_id)
        selector = Filter(
            must=[
                FieldCondition(
                    key="module_id", match=MatchValue(value=scope.module_ids[0])
                )
            ],
            should=[
                FieldCondition(key="resource_id", match=MatchValue(value=resource_id)),
                FieldCondition(
                    key="document_resource_id", match=MatchValue(value=resource_id)
                ),
            ],
        )
        try:
            result = self.client.count(
                collection_name=target, count_filter=selector, exact=True
            )
            return int(getattr(result, "count", 0) or 0)
        except Exception as exc:
            _logger.warning(
                "QdrantCanonicalRuntime: count_resource_points on %s failed: %s",
                target,
                exc,
            )
            return None

    def count_resource_points_batch(
        self,
        module_id: str,
        resource_ids: Iterable[str],
        generation_id: Optional[str] = None,
        max_workers: int = 4,
    ) -> dict[str, Optional[int]]:
        """Per-resource counts for many resources with bounded concurrency.

        P15: the parity sweep used to issue one blocking ``count`` round
        trip per resource serially on the event loop.  Each count keeps the
        exact ``resource_id OR document_resource_id`` selector semantics of
        ``count_resource_points`` — results are identical, only wall time
        collapses.  ``max_workers`` is bounded so a large sweep cannot fan
        out unbounded connections.
        """
        rids = [str(r) for r in resource_ids]
        results: dict[str, Optional[int]] = {rid: None for rid in rids}
        if not rids or not self._healthy or self.client is None:
            return results
        workers = max(1, min(int(max_workers or 4), len(rids)))
        with ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="rag-parity-count"
        ) as pool:
            future_map = {
                pool.submit(
                    self.count_resource_points, module_id, rid, generation_id
                ): rid
                for rid in rids
            }
            for future in as_completed(future_map):
                results[future_map[future]] = future.result()
        return results

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
