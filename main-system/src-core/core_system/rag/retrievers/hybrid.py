"""HybridRetriever — Qdrant Dense + PG FTS + RRF (A52 hybrid-rag).

General knowledge retrieval combining dense vector similarity and
sparse keyword matching via Reciprocal Rank Fusion.  This is the
canonical counterpart of ``LocalRagRetrievalMixin._hybrid_rrf``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..pipeline_retrieval import RerankerFn, reciprocal_rank_fusion

_logger = logging.getLogger("gptbridge.rag.hybrid")

HYBRID_RAG_ID = "hybrid-rag"


@dataclass
class HybridRetrievalRequest:
    """A hybrid retrieval request."""
    query_text: str
    query_embedding: list[float]
    module_ids: tuple[str, ...]
    candidate_limit: int = 24
    top_k: int = 6
    score_threshold: Optional[float] = None


@dataclass
class HybridRetrievalResult:
    """A hybrid retrieval result."""
    sub_architecture: str = HYBRID_RAG_ID
    query: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reranker_meta: dict[str, Any] = field(default_factory=dict)
    retrieval: str = "canonical-qdrant-dense+postgresql-fts+rrf"


class HybridRetriever:
    """Hybrid RAG retriever — Dense + FTS + RRF + optional reranker.

    Delegates to the host pipeline's ``hybrid_search_reranked`` method.
    Does not own a model load (A49).
    """

    def __init__(self, pipeline: Any, reranker: Optional[RerankerFn] = None) -> None:
        self._pipeline = pipeline
        self._reranker = reranker

    def retrieve(self, request: HybridRetrievalRequest) -> HybridRetrievalResult:
        """Execute hybrid retrieval through the canonical pipeline."""
        ranked, meta = self._pipeline.hybrid_search_reranked(
            request.query_embedding,
            request.query_text,
            module_ids=request.module_ids,
            candidate_limit=request.candidate_limit,
            top_k=request.top_k,
            score_threshold=request.score_threshold,
            reranker=self._reranker,
        )
        return HybridRetrievalResult(
            query=request.query_text,
            candidates=ranked,
            reranker_meta=meta,
        )


__all__ = [
    "HYBRID_RAG_ID",
    "HybridRetrievalRequest",
    "HybridRetrievalResult",
    "HybridRetriever",
]
