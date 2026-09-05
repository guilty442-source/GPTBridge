"""Local vector store — canonical implementation in xingcheng infrastructure.

The persistent sqlite implementation lives in ``.vector_store`` (A35/E21,
single local stack).  ``LocalVectorStore`` is re-exported here so existing
``xingcheng`` callers keep their import path.
"""

from __future__ import annotations

from .vector_store import (
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
