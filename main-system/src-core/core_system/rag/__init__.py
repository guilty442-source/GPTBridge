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
    create_rag_pipeline_from_env,
)

from .embeddings import (
    EmbeddingProvider,
    OpenAIEmbeddingProvider,
    OllamaEmbeddingProvider,
    LocalEmbeddingProvider,
    create_embedding_provider_from_env,
)

from .chunking import (
    Chunk,
    Document,
    ChunkingStrategy,
    FixedSizeChunking,
    SemanticChunking,
    ChunkingService,
    create_chunker,
    create_chunking_service_from_env,
)

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