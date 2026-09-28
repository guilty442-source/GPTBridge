"""A371-A374 canonical hybrid retrieval — Dense + PG FTS + RRF + Reranker.

The canonical pipeline must NOT degrade to dense-only retrieval after
canonical takeover.  This mixin adds the hybrid retrieval surface that
``LocalRagService`` already operates in degraded mode:

    vectord Dense  ┐
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

import asyncio
import inspect
import logging
from typing import Any, Callable, Optional

from shared_layer.adaptive.bounded_executor import (
    OverflowPolicy,
    PoolPolicy,
    pool_for,
)
from shared_layer.adaptive.types import PriorityClass

_logger = logging.getLogger("gptbridge.rag")

# RRF constant (standard k=60 from the original paper).
_RRF_K: int = 60

# bounded-concurrency/v1: resolving an awaitable from a synchronous
# caller that lives inside a running loop previously spawned one
# thread per call.  It now enters this dedicated pool instead — worker
# threads own no running loop so ``asyncio.run`` is safe there, and
# pool tasks make progress independently of the caller's pool (no
# circular wait: a loop-resolve task never blocks on rag admission).
_RESOLVE_POOL = PoolPolicy(
    pool="rag.loop-resolve",
    work_class="rag",
    min_workers=1,
    max_workers=4,
    queue_capacity=128,
    deadline_ms=60_000,
    overflow=OverflowPolicy.REJECT,
    backpressure_wait_ms=2_000,
)


def _run_awaitable_sync(awaitable: Any, description: str) -> Any:
    """Resolve one awaitable for a synchronous caller — never a new
    thread per call.  No running loop → ``asyncio.run`` inline; running
    loop → submit to the bounded resolve pool and block on the future.
    Queue-full/deadline propagate as the pool's rejection errors."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    future = pool_for(_RESOLVE_POOL).submit(
        asyncio.run,
        awaitable,
        priority=PriorityClass.INTERACTIVE,
        wait_ms=_RESOLVE_POOL.backpressure_wait_ms,
    )
    try:
        return future.result()
    except BaseException as exc:
        _logger.debug("loop-resolve %s failed: %s", description, exc)
        raise


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


def _resolve(value: Any) -> Any:
    """Resolve an awaitable returned by an async retrieval channel.

    ``hybrid_search`` is a synchronous surface while the dense/FTS channels are
    async coroutines.  With no running event loop the coroutine runs via
    ``asyncio.run``; inside a running loop it runs on a dedicated worker thread
    so a synchronous caller never receives a bare coroutine (which previously
    leaked into RRF as ``'coroutine' object is not iterable``).
    """
    if not inspect.isawaitable(value):
        return value
    return _run_awaitable_sync(value, "channel")


async def _as_coro(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _gather_pair(first: Any, second: Any) -> tuple[Any, Any]:
    """Resolve two channel results concurrently, preserving channel-order
    failure semantics (the first channel's error wins)."""
    first_result, second_result = await asyncio.gather(
        _as_coro(first), _as_coro(second), return_exceptions=True
    )
    if isinstance(first_result, BaseException):
        raise first_result
    if isinstance(second_result, BaseException):
        raise second_result
    return first_result, second_result


def _resolve_pair(first: Any, second: Any) -> tuple[Any, Any]:
    """Resolve two channel results through ONE loop — concurrent when
    both are awaitables (the vectord wait overlaps the FTS query) and
    cheaper than ``_resolve`` twice (one thread/loop, not two)."""
    if not inspect.isawaitable(first) and not inspect.isawaitable(second):
        return first, second
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_gather_pair(first, second))
    return _run_awaitable_sync(_gather_pair(first, second), "channel-pair")


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
        """Canonical hybrid retrieval: vectord Dense + PG FTS + RRF.

        Returns fused chunk-level records sorted by ``rrf_score``.
        Each record carries ``vector_rank``, ``keyword_rank``, and
        ``rrf_score`` for observability.
        """
        # Dense channel (vectord + index_state proof) and sparse channel
        # (PostgreSQL FTS) resolve concurrently through one loop.
        vector_hits, keyword_hits = _resolve_pair(
            self.vector_search(
                query_embedding,
                module_ids=module_ids,
                top_k=candidate_limit,
                score_threshold=score_threshold,
            ),
            self.keyword_search(
                query_text,
                module_ids=module_ids,
                limit=candidate_limit,
            ),
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
            return [], {
                "reranker_applied": False,
                "reason": "no-candidates",
                "fallback": "rrf-hybrid-ranking",
            }
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
