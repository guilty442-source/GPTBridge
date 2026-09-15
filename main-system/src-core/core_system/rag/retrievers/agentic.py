"""AgenticRetriever — retrieve → evaluate → reformulate → retrieve (A52 agentic-rag).

Multi-step retrieval for complex queries.  The agentic loop:
  1. Retrieve initial candidates (hybrid)
  2. Evaluate relevance (score threshold / coverage check)
  3. Reformulate the query if evidence is insufficient
  4. Re-retrieve with the reformulated query
  5. Repeat up to ``max_iterations`` times

The reformulation callable is injected (A49: the pipeline does not own
a model load).  When no reformulator is provided, the loop degrades to
single-pass hybrid retrieval.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..pipeline_retrieval import RerankerFn
from .hybrid import HybridRetriever, HybridRetrievalRequest

_logger = logging.getLogger("gptbridge.rag.agentic")

AGENTIC_RAG_ID = "agentic-rag"

# Default: stop if top candidate score < threshold for two consecutive passes.
_DEFAULT_MIN_EVIDENCE = 3
_DEFAULT_SCORE_THRESHOLD = 0.15
_DEFAULT_MAX_ITERATIONS = 3

# Reformulator signature: (query, candidates, iteration) -> reformulated_query
ReformulatorFn = Callable[
    [str, list[dict[str, Any]], int], str
]


@dataclass
class AgenticRetrievalRequest:
    """An agentic retrieval request."""
    query_text: str
    query_embedding: list[float]
    module_ids: tuple[str, ...]
    candidate_limit: int = 24
    top_k: int = 6
    score_threshold: Optional[float] = None
    max_iterations: int = _DEFAULT_MAX_ITERATIONS
    min_evidence: int = _DEFAULT_MIN_EVIDENCE
    # Initial reformulation hint (optional)
    reformulation_hint: str = ""


@dataclass
class AgenticRetrievalResult:
    """An agentic retrieval result."""
    sub_architecture: str = AGENTIC_RAG_ID
    query: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reranker_meta: dict[str, Any] = field(default_factory=dict)
    iterations: int = 0
    reformulations: list[str] = field(default_factory=list)
    final_query: str = ""
    retrieval: str = "canonical-agentic-multi-step-retrieve+evaluate+reformulate"


class AgenticRetriever:
    """Agentic RAG retriever — multi-step retrieve + evaluate + reformulate.

    Delegates each retrieval pass to ``HybridRetriever`` and uses an
    injected reformulator to refine the query when evidence is
    insufficient.
    """

    def __init__(
        self,
        pipeline: Any,
        reranker: Optional[RerankerFn] = None,
        reformulator: Optional[ReformulatorFn] = None,
    ) -> None:
        self._hybrid = HybridRetriever(pipeline, reranker)
        self._reformulator = reformulator

    def _evaluate_evidence(
        self, candidates: list[dict[str, Any]], request: AgenticRetrievalRequest
    ) -> bool:
        """Check if the current candidates provide sufficient evidence."""
        if len(candidates) < request.min_evidence:
            return False
        threshold = request.score_threshold or _DEFAULT_SCORE_THRESHOLD
        top_score = max(
            (float(c.get("rrf_score") or c.get("reranker_score") or 0.0)
             for c in candidates),
            default=0.0,
        )
        return top_score >= threshold

    def retrieve(self, request: AgenticRetrievalRequest) -> AgenticRetrievalResult:
        """Execute agentic multi-step retrieval."""
        current_query = request.query_text
        current_embedding = request.query_embedding
        reformulations: list[str] = []
        best_candidates: list[dict[str, Any]] = []
        best_meta: dict[str, Any] = {}
        iterations = 0

        for iteration in range(1, request.max_iterations + 1):
            iterations = iteration
            hybrid_req = HybridRetrievalRequest(
                query_text=current_query,
                query_embedding=current_embedding,
                module_ids=request.module_ids,
                candidate_limit=request.candidate_limit,
                top_k=request.candidate_limit,
                score_threshold=request.score_threshold,
            )
            hybrid_result = self._hybrid.retrieve(hybrid_req)
            candidates = hybrid_result.candidates
            best_meta = hybrid_result.reranker_meta

            if self._evaluate_evidence(candidates, request):
                best_candidates = candidates[:request.top_k]
                _logger.debug(
                    "agentic: sufficient evidence at iteration %d (%d candidates)",
                    iteration, len(candidates),
                )
                break

            # Keep the best so far
            if len(candidates) > len(best_candidates):
                best_candidates = candidates[:request.top_k]

            # Reformulate if a reformulator is available
            if self._reformulator is None:
                _logger.debug("agentic: no reformulator, stopping at iteration %d", iteration)
                break

            try:
                reformulated = self._reformulator(current_query, candidates, iteration)
            except Exception as exc:
                _logger.warning("agentic: reformulation failed: %s", exc)
                break

            if not reformulated or reformulated == current_query:
                _logger.debug("agentic: reformulation unchanged, stopping at iteration %d", iteration)
                break

            reformulations.append(reformulated)
            current_query = reformulated
            # Note: in production the embedding would be regenerated here.
            # For now we reuse the original embedding — the reformulator
            # is expected to produce keyword-rich variants that benefit
            # the FTS channel even with the same dense vector.

        return AgenticRetrievalResult(
            query=request.query_text,
            candidates=best_candidates,
            reranker_meta=best_meta,
            iterations=iterations,
            reformulations=reformulations,
            final_query=current_query,
        )


__all__ = [
    "AGENTIC_RAG_ID",
    "AgenticRetrievalRequest",
    "AgenticRetrievalResult",
    "AgenticRetriever",
    "ReformulatorFn",
]
