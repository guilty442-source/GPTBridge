"""A371-A374 canonical hybrid retrieval — Dense + PG FTS + RRF + Reranker.

The canonical pipeline must NOT degrade to dense-only retrieval after
canonical takeover.  This mixin adds the hybrid retrieval surface that
``LocalRagService`` already operates in degraded mode:

    Qdrant Dense  ┐
                   ├─ RRF (Reciprocal Rank Fusion)
    PG FTS        ┘
                   ↓
    Qwen3 Reranker
                   ↓
    Top-K

The mixin is transport-agnostic: it calls ``vector_search`` and
``keyword_search`` on the host pipeline (canonical or degraded) and
applies RRF + an optional reranker.  The reranker is injected — the
canonical pipeline does not own a model load (A49: implementation deps
are not role authority).
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

_logger = logging.getLogger("gptbridge.rag")

# RRF constant (standard k=60 from the original paper).
_RRF_K: int = 60


def reciprocal_rank_fusion(
    *ranked_lists: list[dict[str, Any]],
    key: str = "chunk_id",
) -> list[dict[str, Any]]:
    """Fuse multiple ranked lists into one via Reciprocal Rank Fusion.

    Each input list is already ranked (best first).  The fused score is
    ``sum(1 / (k + rank))`` across all lists where the item appears.
    Items are deduplicated by ``key`` (default ``chunk_id``); the
    highest-scoring record is kept and enriched with ``rrf_score`` and
    per-channel ``<channel>_rank`` fields.
    """
    fused: dict[str, dict[str, Any]] = {}
    for channel_index, ranked in enumerate(ranked_lists):
        channel = _channel_name(channel_index)
        for rank, item in enumerate(ranked, start=1):
            item_key = str(item.get(key) or item.get("point_id") or item.get("id") or "")
            if not item_key:
                continue
            record = fused.setdefault(item_key, dict(item))
            # Merge fields not yet present
            for k, v in item.items():
                if k not in record:
                    record[k] = v
            record["rrf_score"] = float(record.get("rrf_score") or 0.0) + 1.0 / (_RRF_K + rank)
            record[f"{channel}_rank"] = rank
    result = list(fused.values())
    result.sort(key=lambda r: -float(r.get("rrf_score") or 0.0))
    return result


def _channel_name(index: int) -> str:
    return {0: "vector", 1: "keyword"}.get(index, f"channel_{index}")


# Type alias for the reranker callable.
# Signature: (query: str, candidates: list[dict]) -> (ranked, meta)
RerankerFn = Callable[[str, list[dict[str, Any]]], tuple[list[dict[str, Any]], dict[str, Any]]]


class PipelineRetrievalMixin:
    """Hybrid retrieval surface for ``CanonicalRagPipeline``.

    Adds ``hybrid_search`` (Dense + FTS + RRF) and ``hybrid_search_reranked``
    (hybrid + reranker) so the canonical path preserves the same retrieval
    quality as the degraded ``LocalRagService`` path.

    The host class must provide:
      * ``vector_search(query_embedding, *, module_ids, top_k, score_threshold)``
      * ``keyword_search(query, *, module_ids, limit)``
      * ``config`` (with ``embedding_model`` etc.)
    """

    def hybrid_search(
        self,
        query_embedding: list[float],
        query_text: str,
        *,
        module_ids: tuple[str, ...],
        candidate_limit: int = 24,
        score_threshold: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Canonical hybrid retrieval: Qdrant Dense + PG FTS + RRF.

        Returns fused chunk-level records sorted by ``rrf_score``.
        Each record carries ``vector_rank``, ``keyword_rank``, and
        ``rrf_score`` for observability.
        """
        # Dense channel (Qdrant + index_state proof)
        vector_hits = self.vector_search(
            query_embedding,
            module_ids=module_ids,
            top_k=candidate_limit,
            score_threshold=score_threshold,
        )
        # Sparse channel (PostgreSQL FTS)
        keyword_hits = self.keyword_search(
            query_text,
            module_ids=module_ids,
            limit=candidate_limit,
        )
        fused = reciprocal_rank_fusion(vector_hits, keyword_hits)
        _logger.debug(
            "hybrid_search: vector=%d keyword=%d fused=%d",
            len(vector_hits), len(keyword_hits), len(fused),
        )
        return fused

    def hybrid_search_reranked(
        self,
        query_embedding: list[float],
        query_text: str,
        *,
        module_ids: tuple[str, ...],
        candidate_limit: int = 24,
        top_k: int = 6,
        score_threshold: Optional[float] = None,
        reranker: Optional[RerankerFn] = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Hybrid retrieval + optional Qwen3 reranker → top-K.

        Returns (ranked_results, reranker_meta).  When no reranker is
        injected, falls back to RRF ranking and reports
        ``reranker_applied=False``.
        """
        fused = self.hybrid_search(
            query_embedding,
            query_text,
            module_ids=module_ids,
            candidate_limit=candidate_limit,
            score_threshold=score_threshold,
        )
        if not fused:
            return [], {"reranker_applied": False, "reason": "no-candidates"}
        if reranker is None:
            return fused[:top_k], {
                "reranker_applied": False,
                "reason": "no-reranker-injected",
                "fallback": "rrf-hybrid-ranking",
            }
        try:
            ranked, meta = reranker(query_text, fused[:candidate_limit])
        except Exception as exc:
            _logger.warning("hybrid_search_reranked: reranker failed: %s", exc)
            return fused[:top_k], {
                "reranker_applied": False,
                "reason": str(exc),
                "fallback": "rrf-hybrid-ranking",
            }
        return ranked[:top_k], meta


__all__ = [
    "PipelineRetrievalMixin",
    "RerankerFn",
    "reciprocal_rank_fusion",
]
