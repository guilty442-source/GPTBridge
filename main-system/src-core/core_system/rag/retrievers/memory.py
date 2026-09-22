"""MemoryRetriever — session + episodic + long-term memory (A52 memory-rag).

Contextual memory retrieval that combines:
  1. Session memory — current conversation context
  2. Episodic memory — past interaction episodes
  3. Long-term memory — persistent knowledge base entries

Each memory tier is retrieved from the shared Qdrant + PostgreSQL
infrastructure but filtered by memory scope tags in the payload.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from ..pipeline_retrieval import RerankerFn, reciprocal_rank_fusion

_logger = logging.getLogger("gptbridge.rag.memory")

MEMORY_RAG_ID = "memory-rag"

# Memory scope tags stored in Qdrant payload / PostgreSQL metadata.
SCOPE_SESSION = "session"
SCOPE_EPISODIC = "episodic"
SCOPE_LONG_TERM = "long-term"


@dataclass
class MemoryRetrievalRequest:
    """A memory retrieval request."""
    query_text: str
    query_embedding: list[float]
    module_ids: tuple[str, ...]
    session_id: str = ""
    candidate_limit: int = 24
    top_k: int = 6
    score_threshold: Optional[float] = None
    # Which memory scopes to include (default: all three)
    scopes: tuple[str, ...] = (SCOPE_SESSION, SCOPE_EPISODIC, SCOPE_LONG_TERM)


@dataclass
class MemoryRetrievalResult:
    """A memory retrieval result."""
    sub_architecture: str = MEMORY_RAG_ID
    query: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reranker_meta: dict[str, Any] = field(default_factory=dict)
    scopes_used: tuple[str, ...] = ()
    retrieval: str = "canonical-memory-session+episodic+long-term"


class MemoryRetriever:
    """Memory RAG retriever — session + episodic + long-term.

    Delegates dense/keyword search to the host pipeline, then filters
    by memory scope tags.  Session-scoped results are boosted when a
    ``session_id`` is provided.
    """

    def __init__(self, pipeline: Any, reranker: Optional[RerankerFn] = None) -> None:
        self._pipeline = pipeline
        self._reranker = reranker

    @staticmethod
    def _filter_by_scope(
        candidates: list[dict[str, Any]],
        scopes: tuple[str, ...],
        session_id: str = "",
    ) -> list[dict[str, Any]]:
        """Filter candidates by memory scope tags."""
        filtered: list[dict[str, Any]] = []
        for candidate in candidates:
            scope = str(candidate.get("memory_scope") or candidate.get("scope") or "")
            if scope and scope not in scopes:
                continue
            # Session-scoped results must match the session_id
            if scope == SCOPE_SESSION and session_id:
                candidate_session = str(candidate.get("session_id") or "")
                if candidate_session and candidate_session != session_id:
                    continue
            filtered.append(candidate)
        return filtered

    def retrieve(self, request: MemoryRetrievalRequest) -> MemoryRetrievalResult:
        """Execute memory retrieval through the canonical pipeline."""
        # Dense + keyword hybrid search
        vector_hits = self._pipeline.vector_search(
            request.query_embedding,
            module_ids=request.module_ids,
            top_k=request.candidate_limit,
            score_threshold=request.score_threshold,
        )
        keyword_hits = self._pipeline.keyword_search(
            request.query_text,
            module_ids=request.module_ids,
            limit=request.candidate_limit,
        )
        fused = reciprocal_rank_fusion(vector_hits, keyword_hits)
        # Filter by memory scope
        scoped = self._filter_by_scope(fused, request.scopes, request.session_id)
        # If scope filtering removed everything, fall back to unfiltered
        # (the scope tags may not be present in all deployments)
        if not scoped and fused:
            scoped = fused
            _logger.debug("memory: no scope-tagged candidates, using unfiltered")
        # Session boost: candidates matching the session_id get a small boost
        if request.session_id:
            for candidate in scoped:
                if str(candidate.get("session_id") or "") == request.session_id:
                    candidate["rrf_score"] = float(
                        candidate.get("rrf_score") or 0.0
                    ) + 0.05
            scoped.sort(key=lambda r: -float(r.get("rrf_score") or 0.0))
        # Apply reranker if available
        if self._reranker and scoped:
            from ..observability import timed_stage
            with timed_stage("reranker"):
                ranked, meta = self._reranker(request.query_text, scoped[:request.candidate_limit])
        else:
            ranked, meta = scoped[:request.top_k], {
                "reranker_applied": False, "fallback": "rrf+scope-filter+session-boost"
            }
        return MemoryRetrievalResult(
            query=request.query_text,
            candidates=ranked[:request.top_k],
            reranker_meta=meta,
            scopes_used=request.scopes,
        )


__all__ = [
    "MEMORY_RAG_ID",
    "SCOPE_SESSION",
    "SCOPE_EPISODIC",
    "SCOPE_LONG_TERM",
    "MemoryRetrievalRequest",
    "MemoryRetrievalResult",
    "MemoryRetriever",
]
