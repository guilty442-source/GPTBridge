"""RAG Core Protocols — 介面契約定義。

所有 RAG 組件依賴介面而非實作。
CanonicalRagBackend / DegradedRagBackend 實作 RagIndexBackend。
四種 Retriever 實作 RagRetriever。
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, Optional, runtime_checkable

from .rag_contracts import (
    RagIndexRequest,
    RagIndexResult,
    RagDeleteRequest,
    RagDeleteResult,
    RagSearchRequest,
    RagSearchResult,
    RagReconcileRequest,
    RagReconcileResult,
    RagBackendHealth,
    RagRetrievalRequest,
    RagRetrievalResult,
)


# ============================================================================
# Index Backend Protocol
# ============================================================================

@runtime_checkable
class RagIndexBackend(Protocol):
    """Unified index backend interface.

    CanonicalRagBackend: Qdrant (alias) + PostgreSQL + Outbox
    DegradedRagBackend:  LocalVectorStore + LocalSqliteRagRepository
    """

    @abstractmethod
    def health(self) -> RagBackendHealth:
        """Return backend health status."""
        ...

    @abstractmethod
    def upsert_resource(self, request: RagIndexRequest) -> RagIndexResult:
        """Index or update a resource atomically.

        Canonical: PostgreSQL transaction (metadata + outbox) + Qdrant upsert
        Degraded:  LocalVectorStore + LocalSqliteRagRepository
        """
        ...

    @abstractmethod
    def delete_resource(self, request: RagDeleteRequest) -> RagDeleteResult:
        """Delete a resource.

        Canonical: PostgreSQL TOMBSTONED → outbox DELETE → Qdrant delete
        Degraded:  LocalSqliteRagRepository delete
        """
        ...

    @abstractmethod
    def search(self, request: RagSearchRequest) -> RagSearchResult:
        """Search vectors.

        Canonical: Qdrant alias search + GenerationBinder verification
        Degraded:  LocalVectorStore search
        """
        ...

    @abstractmethod
    def reconcile(self, request: RagReconcileRequest) -> RagReconcileResult:
        """Run reconciliation for inconsistencies.

        Canonical: Full reconciliation pipeline
        Degraded:  Local store reconciliation
        """
        ...


# ============================================================================
# Retriever Protocol
# ============================================================================

@runtime_checkable
class RagRetriever(Protocol):
    """Unified retriever interface for different RAG architectures.

    Implementations:
    - HybridRagRetriever: dense + FTS + RRF + reranker
    - CodeRagRetriever:   semantic + symbol + AST + dependency graph
    - AgenticRagRetriever: multi-round with evidence evaluation
    - MemoryRagRetriever: semantic + recency + importance
    """

    @property
    @abstractmethod
    def rag_type(self) -> str:
        """Return retriever type: 'hybrid', 'code', 'agentic', 'memory'."""
        ...

    @abstractmethod
    def retrieve(self, request: RagRetrievalRequest) -> RagRetrievalResult:
        """Execute retrieval for this architecture."""
        ...


# ============================================================================
# Embedding Protocol
# ============================================================================

@runtime_checkable
class EmbeddingProvider(Protocol):
    """Embedding model provider interface."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        ...

    @property
    @abstractmethod
    def dimension(self) -> int:
        ...

    @abstractmethod
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple texts."""
        ...

    @abstractmethod
    def embed_single(self, text: str) -> list[float]:
        """Embed single text."""
        ...


# ============================================================================
# Reranker Protocol
# ============================================================================

@runtime_checkable
class Reranker(Protocol):
    """Cross-encoder reranker interface."""

    @property
    @abstractmethod
    def model_id(self) -> str:
        ...

    @abstractmethod
    def rerank(
        self,
        query: str,
        candidates: list[tuple[str, str]],  # (query, document)
    ) -> list[float]:
        """Return relevance scores for query-document pairs."""
        ...


# ============================================================================
# Metadata Authority Protocol
# ============================================================================

@runtime_checkable
class MetadataAuthority(Protocol):
    """PostgreSQL metadata authority interface."""

    @abstractmethod
    def upsert_resource_metadata(self, resource: Any) -> None:
        ...

    @abstractmethod
    def upsert_chunk_metadata(self, chunks: list[Any]) -> None:
        ...

    @abstractmethod
    def get_resource_metadata(self, module_id: str, resource_id: str) -> Optional[dict]:
        ...

    @abstractmethod
    def get_chunk_metadata(self, chunk_id: str) -> Optional[dict]:
        ...

    @abstractmethod
    def full_text_search(
        self,
        query: str,
        module_id: Optional[str],
        limit: int,
    ) -> list[dict]:
        ...

    @abstractmethod
    def health(self) -> RagBackendHealth:
        ...


# ============================================================================
# Vector Store Protocol
# ============================================================================

@runtime_checkable
class VectorStore(Protocol):
    """Vector store interface (Qdrant or Local)."""

    @abstractmethod
    def upsert(self, points: list[Any]) -> bool:
        ...

    @abstractmethod
    def search(
        self,
        vector: list[float],
        filter: Optional[Any],
        limit: int,
        score_threshold: float,
    ) -> list[Any]:
        ...

    @abstractmethod
    def delete(self, resource_id: str, module_id: str) -> bool:
        ...

    @abstractmethod
    def create_alias(self, alias_name: str, collection_name: str) -> bool:
        ...

    @abstractmethod
    def get_alias_target(self, alias_name: str) -> Optional[str]:
        ...

    @abstractmethod
    def health(self) -> RagBackendHealth:
        ...


# ============================================================================
# Outbox Protocol
# ============================================================================

@runtime_checkable
class OutboxRepository(Protocol):
    """Transactional outbox interface."""

    @abstractmethod
    def create_event(
        self,
        request_id: str,
        operation: str,
        module_id: str,
        resource_id: str,
        generation_id: str,
        source_version: int,
        content_hash: str,
        payload: Optional[dict] = None,
    ) -> None:
        """Create outbox event within current PostgreSQL transaction."""
        ...

    @abstractmethod
    def fetch_pending(self, limit: int) -> list[Any]:
        ...

    @abstractmethod
    def mark_succeeded(self, event_id: str) -> None:
        ...

    @abstractmethod
    def mark_retry(self, event_id: str, error: str) -> None:
        ...

    @abstractmethod
    def mark_dead_letter(self, event_id: str, error: str) -> None:
        ...


# ============================================================================
# Generation Manager Protocol
# ============================================================================

@runtime_checkable
class GenerationManager(Protocol):
    """Index generation lifecycle management."""

    @abstractmethod
    def create_generation(self) -> Any:
        ...

    @abstractmethod
    def ensure_collection(self, generation: Any) -> bool:
        ...

    @abstractmethod
    def verify_generation(self, generation: Any) -> dict:
        ...

    @abstractmethod
    def promote_to_active(self, generation: Any) -> bool:
        ...

    @abstractmethod
    def get_active_generation(self) -> Optional[Any]:
        ...


# ============================================================================
# Health Status
# ============================================================================

@dataclass(frozen=True)
class ComponentHealth:
    """Individual component health."""
    name: str
    healthy: bool
    message: str
    details: dict[str, Any] = None


# ============================================================================
# Protocol Registry
# ============================================================================

class ProtocolRegistry:
    """Runtime protocol implementation registry."""

    def __init__(self) -> None:
        self._backends: dict[str, RagIndexBackend] = {}
        self._retrievers: dict[str, RagRetriever] = {}
        self._embedding: Optional[EmbeddingProvider] = None
        self._reranker: Optional[Reranker] = None
        self._metadata: Optional[MetadataAuthority] = None
        self._vector_store: Optional[VectorStore] = None
        self._outbox: Optional[OutboxRepository] = None
        self._generation: Optional[GenerationManager] = None

    def register_backend(self, name: str, backend: RagIndexBackend) -> None:
        self._backends[name] = backend

    def get_backend(self, name: str) -> Optional[RagIndexBackend]:
        return self._backends.get(name)

    def register_retriever(self, retriever: RagRetriever) -> None:
        self._retrievers[retriever.rag_type] = retriever

    def get_retriever(self, rag_type: str) -> Optional[RagRetriever]:
        return self._retrievers.get(rag_type)

    def set_embedding(self, provider: EmbeddingProvider) -> None:
        self._embedding = provider

    def get_embedding(self) -> Optional[EmbeddingProvider]:
        return self._embedding

    def set_reranker(self, reranker: Reranker) -> None:
        self._reranker = reranker

    def get_reranker(self) -> Optional[Reranker]:
        return self._reranker

    def set_metadata(self, authority: MetadataAuthority) -> None:
        self._metadata = authority

    def get_metadata(self) -> Optional[MetadataAuthority]:
        return self._metadata

    def set_vector_store(self, store: VectorStore) -> None:
        self._vector_store = store

    def get_vector_store(self) -> Optional[VectorStore]:
        return self._vector_store

    def set_outbox(self, outbox: OutboxRepository) -> None:
        self._outbox = outbox

    def get_outbox(self) -> Optional[OutboxRepository]:
        return self._outbox

    def set_generation(self, manager: GenerationManager) -> None:
        self._generation = manager

    def get_generation(self) -> Optional[GenerationManager]:
        return self._generation


PROTOCOL_REGISTRY = ProtocolRegistry()


__all__ = [
    "RagIndexBackend",
    "RagRetriever",
    "EmbeddingProvider",
    "Reranker",
    "MetadataAuthority",
    "VectorStore",
    "OutboxRepository",
    "GenerationManager",
    "ComponentHealth",
    "ProtocolRegistry",
    "PROTOCOL_REGISTRY",
]