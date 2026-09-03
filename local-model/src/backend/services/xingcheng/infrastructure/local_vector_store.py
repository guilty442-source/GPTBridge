"""Unified local vector store — canonical implementation in shared_layer.

Compatibility shim so ``xingcheng`` callers keep importing
``LocalVectorStore`` from the infrastructure package while the canonical
persistent sqlite implementation lives once in
``shared_layer.local.vector_store`` (A35/E21, single local stack).
"""

from __future__ import annotations

from shared_layer.local.vector_store import (
    COLLECTION,
    DEFAULT_ENDPOINT,
    LocalVectorStore,
    VectorStore,
    embed_vector,
)

__all__ = [
    "LocalVectorStore",
    "VectorStore",
    "embed_vector",
    "COLLECTION",
    "DEFAULT_ENDPOINT",
]