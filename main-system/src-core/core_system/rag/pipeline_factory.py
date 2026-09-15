"""Environment-driven canonical RAG pipeline factory (A371-A374).

Defaults encode the governed local embedding contract:
``qwen3-embedding:4b`` via the local Ollama runtime at 2560 dimensions —
the same contract as the canonical ``gptbridge_shared_knowledge``
collection.  Remote embedding providers are never the default.
"""

from __future__ import annotations

import os

from .pipeline import CanonicalRagPipeline
from .rag_qdrant import RagPipelineConfig


def create_rag_pipeline_from_env() -> CanonicalRagPipeline:
    """Create pipeline from environment variables."""
    config = RagPipelineConfig(
        qdrant_url=os.environ.get("QDRANT_URL", "http://localhost:6333"),
        qdrant_api_key=os.environ.get("QDRANT_API_KEY"),
        collection_name=os.environ.get(
            "QDRANT_COLLECTION", "gptbridge_shared_knowledge"
        ),
        postgresql_dsn=os.environ.get("POSTGRESQL_DSN", ""),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "qwen3-embedding:4b"),
        embedding_dimension=int(os.environ.get("EMBEDDING_DIMENSION", "2560")),
        embedding_provider=os.environ.get("EMBEDDING_PROVIDER", "ollama"),
        chunk_size=int(os.environ.get("CHUNK_SIZE", "1200")),
        chunk_overlap=int(os.environ.get("CHUNK_OVERLAP", "200")),
    )
    return CanonicalRagPipeline(config)


__all__ = ["create_rag_pipeline_from_env"]
