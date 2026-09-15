"""RAG Package — Canonical RAG Pipeline (A371-A374).

A371: DEFAULT-PATH: source content > qdrant dense retrieval > PostgreSQL official metadata/FTS/index_state > Python domain model > typed result
A374: Binding order: 1 QDRANT_CANONICAL_RUNTIME > 2 PostgreSQL metadata/FTS/index_state > 3 Python domain model
A373: CANONICAL-TAKEOVER: normal read/write execution must prove Qdrant dense retrieval and PostgreSQL official metadata/FTS/index_state are the live path
A374: INDEX-STATE: every indexed resource/chunk records embedding_model, embedding_dimension, chunk_size, chunk_overlap, indexed_at_utc
"""

from .pipeline import (
    CanonicalRagPipeline,
    RagPipelineConfig,
    IndexState,
    RagQueryResult,
    QdrantCanonicalRuntime,
    PostgreSQLMetadataAuthority,
    PythonDomainModel,
)
from .pipeline_factory import create_rag_pipeline_from_env
from .pipeline_retrieval import PipelineRetrievalMixin, reciprocal_rank_fusion

from .runtime_state import (
    CanonicalCheckError,
    CrossStoreOutbox,
    OutboxStep,
    QueueOperation,
    QueueStatus,
    RagRuntimeState,
    RagRuntimeStateMachine,
    ReconciliationQueue,
    ReconciliationQueueItem,
    SagaResult,
    TombstoneGuard,
    TombstoneRecord,
    TransitionError,
    make_idempotency_key,
)

_EMBEDDINGS_EXPORTS = {
    "EmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "OllamaEmbeddingProvider",
    "LocalEmbeddingProvider",
    "create_embedding_provider_from_env",
}
_CHUNKING_EXPORTS = {
    "Chunk",
    "Document",
    "ChunkingStrategy",
    "FixedSizeChunking",
    "SemanticChunking",
    "ChunkingService",
    "create_chunker",
    "create_chunking_service_from_env",
}


def __getattr__(name: str):
    # Embeddings/chunking providers carry optional third-party dependencies
    # (openai, httpx, tiktoken).  They are loaded lazily so the canonical
    # pipeline stays importable in environments that only run the governed
    # local embedding path (A49: implementation deps are not role authority).
    if name in _EMBEDDINGS_EXPORTS:
        from . import embeddings
        return getattr(embeddings, name)
    if name in _CHUNKING_EXPORTS:
        from . import chunking
        return getattr(chunking, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Pipeline
    "CanonicalRagPipeline",
    "RagPipelineConfig",
    "IndexState",
    "RagQueryResult",
    "QdrantCanonicalRuntime",
    "PostgreSQLMetadataAuthority",
    "PythonDomainModel",
    "create_rag_pipeline_from_env",
    # Hybrid retrieval (A52)
    "PipelineRetrievalMixin",
    "reciprocal_rank_fusion",
    # Runtime state machine (A374)
    "CanonicalCheckError",
    "CrossStoreOutbox",
    "OutboxStep",
    "QueueOperation",
    "QueueStatus",
    "RagRuntimeState",
    "RagRuntimeStateMachine",
    "ReconciliationQueue",
    "ReconciliationQueueItem",
    "SagaResult",
    "TombstoneGuard",
    "TombstoneRecord",
    "TransitionError",
    "make_idempotency_key",
    # Embeddings
    "EmbeddingProvider",
    "OpenAIEmbeddingProvider",
    "OllamaEmbeddingProvider",
    "LocalEmbeddingProvider",
    "create_embedding_provider_from_env",
    # Chunking
    "Chunk",
    "Document",
    "ChunkingStrategy",
    "FixedSizeChunking",
    "SemanticChunking",
    "ChunkingService",
    "create_chunker",
    "create_chunking_service_from_env",
]
