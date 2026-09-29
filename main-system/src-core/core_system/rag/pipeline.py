"""RAG Pipeline — canonical RAG path (A371-A374) + A44 degraded delegation.

A371/A374: vectord dense retrieval > PostgreSQL metadata/FTS/index_state >
Python domain model > typed result.  A373: canonical read/write must prove
both stores are live; DEGRADED state delegates to ``DegradedRagPipeline``.

Single-module layout: the pipeline's retrieval / outbox / maintenance /
documents / recovery surfaces and the domain model + factory all live here —
they are one cohesive unit (the previous mixin split added indirection
without reuse).
"""

from __future__ import annotations

import array
import asyncio
import hashlib
import inspect
import logging
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from shared_layer.adaptive.bounded_executor import (
    OverflowPolicy,
    PoolPolicy,
    pool_for,
)
from shared_layer.adaptive.types import PriorityClass
from shared_layer.local.pg_adapter import connect as pg_connect
from shared_layer.local.local_rag_repository import LocalRagRepository
from shared_layer.local.vector_store import LocalVectorStore
from shared_layer.security.vector_scope import VectorScopeError

from .canonical_vector_runtime import (
    IndexState,
    CanonicalVectorRuntime,
    RagPipelineConfig,
    RagQueryResult,
    sanitize_payload,
)
from .rag_contracts import OutboxOperation, OutboxState
from .rag_metadata import PostgreSQLMetadataAuthority
from .runtime_state import (
    CanonicalCheckError,
    CrossStoreOutbox,
    QueueOperation,
    QueueStatus,
    RagRuntimeState,
    RagRuntimeStateMachine,
    ReconciliationQueue,
    TombstoneGuard,
    TransitionError,
)
from .rust_vector_runtime import select_vector_runtime
from .vector_models import PointStruct

_logger = logging.getLogger("gptbridge.rag")



class PythonDomainModel:
    """A374 Step 3: Python domain model - sole production owner of typed results."""

    def __init__(self, config: RagPipelineConfig) -> None:
        self.config = config

    def build_typed_result(
        self,
        vector_hits: list[dict[str, Any]],
        pg_metadata: dict[str, dict[str, Any]],
        index_states: dict[str, IndexState],
        pg_chunks: Optional[dict[str, dict[str, Any]]] = None,
    ) -> list[RagQueryResult]:
        """Build typed domain results from canonical sources."""
        results = []
        for hit in vector_hits:
            payload = hit.get("payload", {})
            resource_id = payload.get("resource_id") or hit.get("id")
            module_id = payload.get("module_id")

            if not resource_id or not module_id:
                continue

            key = f"{module_id}:{resource_id}"
            index_state = index_states.get(key)
            pg_meta = pg_metadata.get(key, {})

            if not index_state:
                _logger.warning("PythonDomainModel: missing index_state for %s", key)
                continue

            # Content is authoritative in PostgreSQL; vectord payloads never
            # carry it.  Hydrate from the barrier batch when available.
            chunk_row = (pg_chunks or {}).get(str(hit.get("id")), {})
            results.append(RagQueryResult(
                resource_id=resource_id,
                module_id=module_id,
                content=str(chunk_row.get("content") or payload.get("content") or ""),
                score=hit.get("score", 0.0),
                metadata={**payload, **pg_meta.get("metadata", {})},
                index_state=index_state,
            ))
        return results




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



_OUTBOX_MAX_ATTEMPTS = 5
_OUTBOX_RETRY_BASE_SECONDS = 2.0

# A record tombstone/provenance update cannot physically delete; purge is
# a separate, explicit operation.
_PURGE_REASON = "purged"


class PipelineOutboxMixin:
    """Canonical outbox + tombstone/delete orchestration (RAG-08/10)."""

    # -- event construction ---------------------------------------------------

    @staticmethod
    def _new_outbox_event(
        *,
        operation: OutboxOperation,
        module_id: str,
        resource_id: str,
        source_version: int = 0,
        content_hash: str = "",
        generation_id: str = "",
        request_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "event_id": str(uuid.uuid4()),
            "request_id": request_id or str(uuid.uuid4()),
            "operation": operation.value,
            "module_id": module_id,
            "resource_id": resource_id,
            "source_version": int(source_version),
            "content_hash": content_hash,
            "generation_id": generation_id,
            "payload": payload or {},
        }

    # -- event application -----------------------------------------------------

    async def _apply_outbox_event(self, event: dict[str, Any]) -> bool:
        """Apply one outbox event to vectord + index_state writeback.

        All branches are idempotent: replays converge to the same state.
        """
        op = str(event.get("operation") or "")
        module_id = str(event.get("module_id") or "")
        resource_id = str(event.get("resource_id") or "")
        generation_id = str(event.get("generation_id") or "") or None
        payload = event.get("payload") or {}

        if op in (
            OutboxOperation.DELETE_RESOURCE.value,
            OutboxOperation.DELETE.value,
        ):
            # delete_resource verifies zero remaining points itself
            return await self.vector.delete_resource(
                module_id=module_id,
                resource_id=resource_id,
                generation_id=generation_id,
            )

        if op not in (
            OutboxOperation.UPSERT_RESOURCE.value,
            OutboxOperation.REINDEX_RESOURCE.value,
            OutboxOperation.RECONCILE_RESOURCE.value,
            OutboxOperation.UPSERT.value,
            OutboxOperation.REINDEX.value,
            OutboxOperation.UPDATE_METADATA.value,
        ):
            _logger.warning("PipelineOutbox: unknown operation %s", op)
            return False

        # UPSERT family: rebuild points from PostgreSQL truth (never from
        # event payload vectors — payloads carry references only).
        chunks = await self.postgresql.fetch_resource_chunks(
            module_id, resource_id
        )
        wanted_ids = [str(i) for i in payload.get("chunk_ids") or ()]
        if wanted_ids:
            chunks = [c for c in chunks if c["chunk_id"] in wanted_ids]
        if not chunks:
            _logger.warning(
                "PipelineOutbox: no canonical chunks for %s:%s",
                module_id, resource_id,
            )
            return False

        # B61/C56: canonical embeddings live on the PostgreSQL chunk row.
        # Chunks that already carry a stored vector are projected as a
        # copy; only chunks missing one (rows predating the canonical
        # embedding column) are re-embedded and backfilled into PG first.
        vectors: list[Any] = [c.get("embedding") for c in chunks]
        missing = [i for i, v in enumerate(vectors) if v is None]
        if missing:
            texts = [str(chunks[i].get("content") or "") for i in missing]
            # PERF-07: the owning engine produces canonical f64-le records
            # first; the injected provider stays the capability fallback.
            fresh = None
            embed_engine = getattr(self.vector, "embed_texts", None)
            if embed_engine is not None:
                fresh = list(await embed_engine(texts) or [])
            if not fresh:
                if self._embed_texts is None:
                    raise RuntimeError(
                        "outbox replay requires embed_texts for chunks "
                        "missing a canonical embedding"
                    )
                fresh = list(
                    await self._call_maybe_async(self._embed_texts, texts) or []
                )
            if len(fresh) != len(missing):
                return False
            for i, vector in zip(missing, fresh):
                vectors[i] = vector
            await self.postgresql.store_chunk_embeddings(
                module_id, resource_id,
                {chunks[i]["chunk_id"]: vectors[i] for i in missing},
            )
        for vector in vectors:
            dim = (
                len(vector) // 8
                if isinstance(vector, (bytes, bytearray, memoryview))
                else len(vector)
            )
            if dim != self.config.embedding_dimension:
                raise RuntimeError(
                    f"EMBEDDING_DIMENSION_MISMATCH: {dim}-dim cannot "
                    f"enter {self.config.embedding_dimension}-dim collection"
                )

        # PERF-07: when the engine owns the embedder and every chunk row
        # carries content, vectord re-derives the projection in-engine —
        # no vector JSON crosses the boundary on replay either.
        text_points = None
        if getattr(self.vector, "upsert_text_points", None) is not None and all(
            str(c.get("content") or "") for c in chunks
        ):
            text_points = [
                {
                    "id": str(c.get("vector_point_id") or c.get("point_id")),
                    "text": str(c["content"]),
                    "payload": sanitize_payload(
                        {
                            "module_id": module_id,
                            "document_resource_id": resource_id,
                            "chunk_id": c["chunk_id"],
                            "generation_id": generation_id or "",
                            "content_hash": c.get("payload", {}).get(
                                "content_hash", ""
                            ),
                        }
                    ),
                }
                for c in chunks
            ]
        upserted = False
        if text_points is not None:
            upserted = await self.vector.upsert_text_points(
                text_points, generation_id=generation_id
            )
        if not upserted:
            # Capability/transport fallback: vector-bearing upsert keeps the
            # replay contract alive on engines without the text endpoint.
            points = [
                PointStruct(
                    id=str(c.get("vector_point_id") or c.get("point_id")),
                    vector=_record_to_floats(vector),
                    payload=sanitize_payload(
                        {
                            "module_id": module_id,
                            "document_resource_id": resource_id,
                            "chunk_id": c["chunk_id"],
                            "generation_id": generation_id or "",
                            "content_hash": c.get("payload", {}).get(
                                "content_hash", ""
                            ),
                        }
                    ),
                )
                for c, vector in zip(chunks, vectors)
            ]
            upserted = await self.vector.upsert_points(
                points, generation_id=generation_id
            )
        if not upserted:
            return False
        # index_state writeback only after vectord confirms
        await self.postgresql.upsert_index_state(
            self._outbox_index_state(event, chunks),
        )
        return True

    def _outbox_index_state(self, event: dict[str, Any], chunks: list) -> Any:
        from .canonical_vector_runtime import IndexState

        first = chunks[0] if chunks else {}
        return IndexState(
            resource_id=str(event["resource_id"]),
            module_id=str(event["module_id"]),
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            chunk_size=int(self.config.chunk_size),
            chunk_overlap=int(self.config.chunk_overlap),
            indexed_at_utc=datetime.now(timezone.utc).isoformat(),
            content_hash=str(event.get("content_hash") or ""),
            vector_point_id=str(
                first.get("vector_point_id") or first.get("point_id") or ""
            ),
            postgresql_record_id=str(event["resource_id"]),
            source_revision=int(event.get("source_version") or 1),
        )

    # -- replay driver ----------------------------------------------------------

    async def process_outbox(self, batch: int = 25) -> dict[str, int]:
        """Drain pending/due outbox events — idempotent, restart-safe.

        Called inline on the write path after the canonical transaction
        commits, and by recovery after a crash.  Bounded batch; retry with
        exponential backoff; DEAD_LETTER is observable via outbox_stats.
        """
        stats = {"processed": 0, "succeeded": 0, "retried": 0,
                 "dead_lettered": 0}
        fetch = getattr(self.postgresql, "fetch_outbox_events", None)
        if fetch is None:
            return stats
        events = await fetch(batch)
        succeeded_ids: list[str] = []
        batch_mark = getattr(self.postgresql, "mark_outbox_succeeded", None)
        for event in events:
            stats["processed"] += 1
            event_id = str(event["event_id"])
            attempts = int(event.get("attempt_count") or 0) + 1
            try:
                ok = await self._apply_outbox_event(event)
            except Exception as exc:
                ok = False
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                last_error = "apply returned False"
            if ok:
                if batch_mark is not None:
                    succeeded_ids.append(event_id)
                else:
                    await self.postgresql.mark_outbox(
                        event_id, OutboxState.SUCCEEDED.value, terminal=True
                    )
                stats["succeeded"] += 1
                continue
            if attempts >= _OUTBOX_MAX_ATTEMPTS:
                await self.postgresql.mark_outbox(
                    event_id, OutboxState.DEAD_LETTER.value,
                    error=last_error, terminal=True,
                )
                stats["dead_lettered"] += 1
                _logger.error(
                    "PipelineOutbox: %s DEAD_LETTER after %d attempts: %s",
                    event_id, attempts, last_error,
                )
            else:
                delay = _OUTBOX_RETRY_BASE_SECONDS ** attempts
                retry_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=delay)
                ).isoformat()
                await self.postgresql.mark_outbox(
                    event_id, OutboxState.RETRY.value,
                    error=last_error, next_retry_at=retry_at,
                )
                stats["retried"] += 1
        if succeeded_ids and batch_mark is not None:
            # G102: one UPDATE for the whole successful batch instead of
            # per-event round trips.  Apply is idempotent (vectord upsert /
            # delete replay), so a crash before this flush simply replays.
            await batch_mark(succeeded_ids)
        return stats

    # -- RAG-10: tombstone / delete guarantee -----------------------------------

    async def delete_resource(
        self,
        *,
        module_id: str,
        resource_id: str,
        source_version: int = 0,
        content_hash: str = "",
        generation_id: str = "",
        request_id: Optional[str] = None,
        purge: bool = False,
    ) -> bool:
        """Tombstone-first delete guarantee.

        Order: PG tombstone (logical invisibility, immediate) → canonical
        outbox DELETE_RESOURCE → vectord delete (verified) → degraded SQLite
        delete → provenance derived marked stale → index_state='deleted'.

        A failed vectord delete leaves the event PENDING for replay and can
        never resurrect the resource — the read barrier reads the
        tombstone, not the vector index.  ``purge=True`` is a separate,
        explicit physical purge — never implied by delete.
        """
        module_id = str(module_id)
        resource_id = str(resource_id)
        if not module_id or not resource_id:
            return False

        # 1. Authoritative tombstone first — read barrier applies instantly.
        await self.postgresql.raise_tombstone(
            module_id=module_id,
            resource_id=resource_id,
            source_revision=source_version,
            content_hash=content_hash,
            reason=_PURGE_REASON if purge else "deleted",
        )

        # 2. Derived/provenance resources must not stay canonical evidence.
        mark_stale = getattr(self.postgresql, "mark_derived_stale", None)
        if mark_stale is not None:
            await mark_stale(module_id, resource_id)

        # 3. Durable outbox event so the physical delete survives crashes.
        event = self._new_outbox_event(
            operation=OutboxOperation.DELETE_RESOURCE,
            module_id=module_id,
            resource_id=resource_id,
            source_version=source_version,
            content_hash=content_hash,
            generation_id=generation_id,
            request_id=request_id,
        )
        insert = getattr(self.postgresql, "insert_outbox_event", None)
        if insert is not None:
            await insert(event)

        # 4. Physical delete attempt — failure is fine: tombstone holds.
        applied = await self._apply_outbox_event(event)
        if applied:
            mark = getattr(self.postgresql, "mark_index_state", None)
            if mark is not None:
                await mark(module_id, resource_id, "deleted")
            await self.postgresql.mark_outbox(
                event["event_id"], OutboxState.SUCCEEDED.value,
                terminal=True,
            )
        else:
            # Event remains PENDING — replayed by process_outbox once
            # vectord is reachable; the tombstone keeps reads closed.
            await self.postgresql.mark_outbox(
                event["event_id"], OutboxState.RETRY.value,
                error="vector delete pending",
                next_retry_at=(
                    datetime.now(timezone.utc)
                    + timedelta(seconds=_OUTBOX_RETRY_BASE_SECONDS)
                ).isoformat(),
            )

        # 5. Degraded SQLite mirror delete (bounded fallback store).
        if self._degraded_pipeline is not None:
            try:
                store = getattr(self._degraded_pipeline, "vector_store", None)
                deleter = getattr(store, "delete", None) or getattr(
                    store, "delete_resource", None
                )
                if deleter is not None:
                    deleter(resource_id, module_id=module_id)
            except Exception as exc:
                _logger.warning(
                    "PipelineOutbox: degraded delete failed for %s:%s: %s",
                    module_id, resource_id, exc,
                )
        return True



class PipelineMaintenanceMixin:
    """Bounded canonical maintenance cycle (lease recovery / outbox drain /
    tombstone purge / parity repair)."""

    # Lease horizon must exceed the slowest legitimate apply attempt
    # (vectord client timeout is 30 s) so a live worker's lease is never
    # reclaimed mid-flight.
    _OUTBOX_STALE_LEASE_S = int(
        os.environ.get("RAG_OUTBOX_STALE_LEASE_S", "300")
    )
    _MAINTENANCE_BUDGET_S = float(
        os.environ.get("RAG_MAINTENANCE_BUDGET_S", "30")
    )
    # Tombstones must outlive their retention horizon before physical
    # purge is even considered — purge lifts the read barrier.
    _TOMBSTONE_PURGE_AFTER_S = int(
        os.environ.get("RAG_TOMBSTONE_PURGE_AFTER_S", "3600")
    )
    _TOMBSTONE_PURGE_BATCH = int(
        os.environ.get("RAG_TOMBSTONE_PURGE_BATCH", "50")
    )

    async def run_maintenance_cycle(
        self,
        *,
        budget_seconds: Optional[float] = None,
        include_parity: bool = False,
    ) -> dict[str, Any]:
        """One bounded maintenance pass over the canonical RAG stores.

        Fail-closed: outside CANONICAL the cycle is a no-op (recovery owns
        repair while DEGRADED); every individual step degrades to a counted
        no-op when the authority is unavailable.  ``include_parity`` adds
        the incremental index_state↔vectord sweep with drain — callers keep
        it on a slower cadence than the outbox pass.
        """
        started = time.monotonic()
        budget = float(
            budget_seconds
            if budget_seconds is not None
            else self._MAINTENANCE_BUDGET_S
        )
        deadline = started + budget
        report: dict[str, Any] = {
            "budget_seconds": budget,
            "duration_ms": 0,
            "budget_exceeded": False,
        }
        state = self._state_machine.state
        if state != RagRuntimeState.CANONICAL:
            report["skipped"] = f"state={state.value}"
            report["duration_ms"] = int((time.monotonic() - started) * 1000)
            return report

        # 1. Crash recovery — orphaned PROCESSING leases back to RETRY
        #    (or DEAD_LETTER once attempts are exhausted).
        reset = getattr(self.postgresql, "reset_stale_outbox_leases", None)
        if reset is not None:
            try:
                report["lease_recovery"] = await reset(
                    stale_after_seconds=self._OUTBOX_STALE_LEASE_S,
                    max_attempts=_OUTBOX_MAX_ATTEMPTS,
                )
            except Exception as exc:  # noqa: BLE001 — maintenance must not raise
                report["lease_recovery"] = {"error": str(exc)}

        # 2. Resume unfinished work — pending + due-retry + reclaimed leases.
        if time.monotonic() < deadline:
            try:
                report["outbox"] = await self.process_outbox()
            except Exception as exc:  # noqa: BLE001
                report["outbox"] = {"error": str(exc)}

        # 3. Tombstone closure — physical purge only after verified-empty
        #    vector state; residue is repaired, never purged.
        if time.monotonic() < deadline:
            report["tombstones"] = await self.purge_tombstones()

        # 4. Optional incremental parity sweep + repair drain (slow cadence).
        if include_parity and time.monotonic() < deadline:
            try:
                report["parity"] = await self.run_parity_sweep(drain=True)
            except Exception as exc:  # noqa: BLE001
                report["parity"] = {"error": str(exc)}

        elapsed = time.monotonic() - started
        report["duration_ms"] = int(elapsed * 1000)
        report["budget_exceeded"] = time.monotonic() > deadline
        if report["budget_exceeded"]:
            _logger.warning(
                "PipelineMaintenance: cycle exceeded budget %.1fs", budget
            )
        return report

    async def purge_tombstones(
        self,
        *,
        older_than_seconds: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict[str, int]:
        """Tombstone cleanup closure (RAG-10).

        For each aged, unpurged tombstone:
          * vectord point count unverifiable → keep tombstone (fail-closed).
          * Residual points > 0 → enqueue a durable DELETE_RESOURCE outbox
            event (orphan repair); the tombstone stays until the physical
            delete is verified — never purge over live residue.
          * Zero points → purge canonical metadata (chunks + index_state)
            and mark the tombstone ``purged``.

        Bounded by ``limit``; retention horizon ``older_than_seconds``
        keeps recently-tombstoned resources recoverable.
        """
        stats = {
            "listed": 0, "purged": 0, "residue_repaired": 0,
            "unverifiable": 0, "failed": 0, "unavailable": 0,
        }
        listing = getattr(self.postgresql, "list_unpurged_tombstones", None)
        purge_md = getattr(self.postgresql, "purge_tombstone_metadata", None)
        insert = getattr(self.postgresql, "insert_outbox_event", None)
        if listing is None or purge_md is None:
            stats["unavailable"] = 1
            return stats
        rows = await listing(
            older_than_seconds=int(
                older_than_seconds
                if older_than_seconds is not None
                else self._TOMBSTONE_PURGE_AFTER_S
            ),
            limit=int(
                limit if limit is not None else self._TOMBSTONE_PURGE_BATCH
            ),
        )
        if rows is None:
            stats["unavailable"] = 1
            return stats
        for row in rows:
            stats["listed"] += 1
            module_id = str(row["module_id"])
            resource_id = str(row["resource_id"])
            try:
                remaining = self.vector.count_resource_points(
                    module_id, resource_id
                )
            except Exception:  # noqa: BLE001
                remaining = None
            if remaining is None:
                stats["unverifiable"] += 1
                continue
            if remaining > 0:
                repaired = False
                if insert is not None:
                    event = self._new_outbox_event(
                        operation=OutboxOperation.DELETE_RESOURCE,
                        module_id=module_id,
                        resource_id=resource_id,
                        source_version=int(row.get("source_revision") or 0),
                        content_hash=str(row.get("content_hash") or ""),
                    )
                    repaired = bool(await insert(event))
                stats["residue_repaired" if repaired else "failed"] += 1
                continue
            if await purge_md(module_id, resource_id):
                stats["purged"] += 1
            else:
                stats["failed"] += 1
        return stats



def _record_to_floats(record: Any) -> list[float]:
    """Decode a canonical f64-le embedding record; float lists pass through."""
    if isinstance(record, (bytes, bytearray, memoryview)):
        vector = array.array("d")
        vector.frombytes(bytes(record))
        if sys.byteorder == "big":  # pragma: no cover - governed host is LE
            vector.byteswap()
        return list(vector)
    return [float(v) for v in record]


class PipelineDocumentsMixin:
    """Document write flow + tombstone gate + index_state writeback."""

    async def index_document(
        self,
        *,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        collection_dimension: Optional[int] = None,
    ) -> bool:
        """Document-level canonical write (A371-A374).

        ``chunks`` carry deterministic ``vector_point_id``/``point_id``
        UUIDs and self-describing payloads.
        """
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        await self.attempt_recovery()
        module_id = str(document["module_id"])
        resource_id = str(document["resource_id"])
        if self.state == RagRuntimeState.DEGRADED:
            # A374: record the pending_rag_mutation so recovery replays it
            # through the real re-fetch → re-chunk → re-embed → write flow.
            await self._enqueue_document_mutation(document, chunks)
            return False
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        if collection_dimension:
            self.config.embedding_dimension = int(collection_dimension)

        if await self._tombstoned(module_id, resource_id):
            return False

        embedding_model = str(
            document.get("embedding_model") or self.config.embedding_model
        )
        if not await self._canonical_document_write(
            document, chunks, vectors, resource_id, module_id,
            embedding_model, collection_dimension,
        ):
            # Any failed canonical step leaves a durable pending mutation
            # so recovery replays the whole write idempotently.
            await self._enqueue_document_mutation(document, chunks)
            return False
        return True

    async def index_document_text(
        self,
        *,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        embedding_records: list[Any],
        collection_dimension: Optional[int] = None,
    ) -> bool:
        """Text-mode canonical write (PERF-07).

        ``embedding_records`` are canonical f64-le byte records produced by
        the owning engine's ``/v1/embed`` — they are bound straight into the
        PostgreSQL chunk authority (B61/C56) without materialising a float
        list, and vectord re-derives the identical vectors from the chunk
        ``content`` for its derived projection.  ``None``-capable clients
        fall back to :meth:`index_document` at the caller.
        """
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        await self.attempt_recovery()
        module_id = str(document["module_id"])
        resource_id = str(document["resource_id"])
        if self.state == RagRuntimeState.DEGRADED:
            await self._enqueue_document_mutation(document, chunks)
            return False
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        if collection_dimension:
            self.config.embedding_dimension = int(collection_dimension)
        if len(embedding_records) != len(chunks):
            raise RuntimeError("RAG_EMBEDDING_COUNT_MISMATCH")

        if await self._tombstoned(module_id, resource_id):
            return False

        embedding_model = str(
            document.get("embedding_model") or self.config.embedding_model
        )
        if not await self._canonical_document_write_text(
            document, chunks, embedding_records, resource_id, module_id,
            embedding_model, collection_dimension,
        ):
            await self._enqueue_document_mutation(document, chunks)
            return False
        return True

    async def _canonical_document_write_text(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        embedding_records: list[Any],
        resource_id: str,
        module_id: str,
        embedding_model: str,
        collection_dimension: Optional[int],
    ) -> bool:
        """Text-mode twin of :meth:`_canonical_document_write`: PG tx binds
        the canonical embedding bytes; the vectord upsert is text-bearing so
        the engine embeds in-process (no vector JSON on the wire)."""
        event = self._new_outbox_event(
            operation=OutboxOperation.UPSERT_RESOURCE,
            module_id=module_id,
            resource_id=resource_id,
            source_version=int(document.get("version") or 0),
            content_hash=str(
                document.get("sha256") or document.get("content_hash") or ""
            ),
            generation_id=str(document.get("generation_id") or ""),
            request_id=str(document.get("request_id") or "") or None,
            payload={
                "chunk_ids": [str(c.get("chunk_id")) for c in chunks],
            },
        )
        # B61/C56: canonical PG transaction carries the embedding bytes.
        for chunk, record in zip(chunks, embedding_records):
            chunk["embedding"] = record
        write_tx = getattr(self.postgresql, "document_write_tx", None)
        if write_tx is not None:
            if not await write_tx(
                document=document,
                chunks=chunks,
                embedding_model=embedding_model,
                outbox_event=event,
            ):
                return False
        else:
            if not await self._pg_document_writes(
                document, chunks, resource_id, module_id, embedding_model
            ):
                return False
            insert = getattr(self.postgresql, "insert_outbox_event", None)
            if insert is not None:
                await insert(event)
        if not await self.vector.ensure_collection(collection_dimension):
            return False
        points = self._document_text_points(document, chunks, module_id, resource_id)
        upserted = bool(points) and await self.vector.upsert_text_points(points)
        if not upserted:
            # Capability fallback: engines without the text endpoint still
            # take the vector-bearing upsert built from the same records.
            vector_points = self._document_points(
                document, chunks,
                [_record_to_floats(record) for record in embedding_records],
                module_id, resource_id,
            )
            upserted = bool(vector_points) and await self.vector.upsert_points(
                vector_points
            )
        if not upserted:
            mark = getattr(self.postgresql, "mark_outbox", None)
            if mark is not None:
                await mark(
                    event["event_id"], OutboxState.RETRY.value,
                    error="vector upsert pending",
                    next_retry_at=datetime.now(timezone.utc).isoformat(),
                )
            return False
        mark = getattr(self.postgresql, "mark_outbox", None)
        if mark is not None:
            await mark(event["event_id"], OutboxState.SUCCEEDED.value,
                       terminal=True)
        return await self._writeback_index_state(
            document, chunks, resource_id, module_id, embedding_model,
            collection_dimension,
        )

    def _document_text_points(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        module_id: str,
        resource_id: str,
    ) -> list[dict[str, Any]]:
        """Text-bearing vectord points — the engine embeds ``text`` itself."""
        return [
            {
                "id": str(chunk.get("vector_point_id") or chunk.get("point_id")),
                "text": str(chunk.get("content") or ""),
                "payload": sanitize_payload(
                    {
                        "module_id": module_id,
                        "document_resource_id": resource_id,
                        "document_id": document.get("document_id"),
                        "indexed_at_utc": datetime.now(timezone.utc).isoformat(),
                        **(chunk.get("payload") or {}),
                    }
                ),
            }
            for chunk in chunks
        ]

    async def _canonical_document_write(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        resource_id: str,
        module_id: str,
        embedding_model: str,
        collection_dimension: Optional[int],
    ) -> bool:
        """PG authority writes + outbox event (ONE transaction) ->
        vectord upsert -> outbox SUCCEEDED + index_state writeback.

        RAG-08: the outbox event commits with the metadata, so a crash
        after commit leaves a durable PENDING event that ``process_outbox``
        replays idempotently.  vectord is never part of the PG transaction.
        """
        event = self._new_outbox_event(
            operation=OutboxOperation.UPSERT_RESOURCE,
            module_id=module_id,
            resource_id=resource_id,
            source_version=int(document.get("version") or 0),
            content_hash=str(
                document.get("sha256") or document.get("content_hash") or ""
            ),
            generation_id=str(document.get("generation_id") or ""),
            request_id=str(document.get("request_id") or "") or None,
            payload={
                "chunk_ids": [str(c.get("chunk_id")) for c in chunks],
            },
        )
        # B61/C56: the canonical PostgreSQL transaction carries the
        # embedding values themselves — the vectord upsert below is a
        # derived projection of what PG already holds.
        for chunk, vector in zip(chunks, vectors):
            chunk["embedding"] = vector
        write_tx = getattr(self.postgresql, "document_write_tx", None)
        if write_tx is not None:
            if not await write_tx(
                document=document,
                chunks=chunks,
                embedding_model=embedding_model,
                outbox_event=event,
            ):
                return False
        else:
            if not await self._pg_document_writes(
                document, chunks, resource_id, module_id, embedding_model
            ):
                return False
            insert = getattr(self.postgresql, "insert_outbox_event", None)
            if insert is not None:
                await insert(event)
        # Step 3: vectord dense vector write (canonical semantic index).
        if not await self.vector.ensure_collection(collection_dimension):
            return False
        points = self._document_points(document, chunks, vectors, module_id, resource_id)
        if not (points and await self.vector.upsert_points(points)):
            # Outbox event stays PENDING — replay applies the vector write.
            mark = getattr(self.postgresql, "mark_outbox", None)
            if mark is not None:
                await mark(
                    event["event_id"], OutboxState.RETRY.value,
                    error="vector upsert pending",
                    next_retry_at=datetime.now(timezone.utc).isoformat(),
                )
            return False
        # Step 4: outbox SUCCEEDED + vector_point_id writeback.
        mark = getattr(self.postgresql, "mark_outbox", None)
        if mark is not None:
            await mark(event["event_id"], OutboxState.SUCCEEDED.value,
                       terminal=True)
        return await self._writeback_index_state(
            document, chunks, resource_id, module_id, embedding_model,
            collection_dimension,
        )

    async def _tombstoned(self, module_id: str, resource_id: str) -> bool:
        """A374: reject stale writes against an existing tombstone."""
        if await self.postgresql.is_tombstoned(module_id, resource_id):
            _logger.warning(
                "CanonicalRagPipeline: reject index_document for tombstoned %s:%s",
                module_id, resource_id,
            )
            return True
        return False

    async def _pg_document_writes(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        resource_id: str,
        module_id: str,
        embedding_model: str,
    ) -> bool:
        """Steps 1-2: resource row, then chunk rows in the PG authority."""
        if not await self.postgresql.ensure_resource(document):
            return False
        return await self.postgresql.replace_document_chunks(
            resource_id=resource_id,
            module_id=module_id,
            embedding_model=embedding_model,
            chunks=chunks,
        )

    def _document_points(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        module_id: str,
        resource_id: str,
    ) -> list[PointStruct]:
        """Build vectord PointStructs for a document's chunks."""
        return [
            PointStruct(
                id=str(chunk.get("vector_point_id") or chunk.get("point_id")),
                vector=[float(v) for v in vector],
                payload=sanitize_payload(
                    {
                        "module_id": module_id,
                        "document_resource_id": resource_id,
                        "document_id": document.get("document_id"),
                        "indexed_at_utc": datetime.now(timezone.utc).isoformat(),
                        **(chunk.get("payload") or {}),
                    }
                ),
            )
            for chunk, vector in zip(chunks, vectors)
        ]

    async def _writeback_index_state(
        self,
        document: dict[str, Any],
        chunks: list[dict[str, Any]],
        resource_id: str,
        module_id: str,
        embedding_model: str,
        collection_dimension: Optional[int],
    ) -> bool:
        """Step 4: write index_state back to PostgreSQL after vectord confirms."""
        first_point = str(chunks[0].get("vector_point_id") or chunks[0].get("point_id")) if chunks else ""
        state = IndexState(
            resource_id=resource_id,
            module_id=module_id,
            embedding_model=embedding_model,
            embedding_dimension=int(collection_dimension or self.config.embedding_dimension),
            chunk_size=int(self.config.chunk_size),
            chunk_overlap=int(self.config.chunk_overlap),
            indexed_at_utc=datetime.now(timezone.utc).isoformat(),
            content_hash=str(document.get("sha256") or document.get("content_hash") or ""),
            vector_point_id=first_point,
            postgresql_record_id=resource_id,
            source_revision=int(document.get("version") or 1),
        )
        return await self.postgresql.upsert_index_state(
            state,
            collection_name=self.config.collection_name,
            chunk_count=len(chunks),
        )


_POINT_NAMESPACE = uuid.UUID("a374b1c0-9e2f-4d6a-8c3e-5f7a9b1d2e4f")
_MAX_ATTEMPTS = 8
_RETRY_BASE_SECONDS = 5.0


class PipelineRecoveryMixin:
    """Recovery driver + reconciliation replay + health reporting."""

    # -- recovery driver ----------------------------------------------------

    async def attempt_recovery(self) -> RagRuntimeState:
        """A374 recovery: DEGRADED → RECONCILING → CANONICAL only after
        queue replay + parity + queue-drain verification; failure stays
        bounded at DEGRADED and is surfaced as RECONCILIATION_FAILED.
        No-op outside DEGRADED or while canonical services are down."""
        if self._blocked_reason:
            # Hard contract violation (dimension mismatch, non-loopback URL,
            # unverifiable contract): surface BLOCKED, never self-heal into
            # RECONCILING/CANONICAL on service health alone.
            return self.state
        if self.state != RagRuntimeState.DEGRADED:
            return self.state
        if not (self.vector.is_healthy() and self.postgresql.is_healthy()):
            return self.state
        try:
            self._state_machine.begin_reconciliation(
                vector_healthy=True, postgresql_healthy=True
            )
        except (CanonicalCheckError, TransitionError) as exc:
            self._state_machine.report_canonical_failure(
                f"reconciliation could not start: {exc}"
            )
            return self.state
        try:
            await self.run_reconciliation()
            parity = await self._recovery_parity()
        except Exception as exc:
            return self._state_machine.fail_reconciliation(str(exc))
        return self._state_machine.complete_reconciliation(**parity)

    async def _recovery_parity(self) -> dict[str, bool]:
        """Honest parity: index_state chunk coverage vs vectord points,
        plus point-id / content-hash / embedding-version completeness."""
        summary = await self.postgresql.index_state_summary(
            self.config.embedding_model, self.config.embedding_dimension
        )
        points = self.vector.points_count()
        if summary is None or points is None:
            raise CanonicalCheckError("parity inputs unavailable")
        return {
            "counts_match": summary["total_chunks"] == points,
            "ids_match": summary["missing_point_ids"] == 0,
            "hashes_match": summary["missing_hashes"] == 0,
            "versions_match": summary["mismatched_versions"] == 0,
        }

    # -- pending_rag_mutation recording ------------------------------------

    async def _enqueue_degraded_write(
        self,
        *,
        module_id: str,
        resource_id: str,
        locator_id: str,
        source_revision: int,
        content_hash: str,
        operation: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record a pending_rag_mutation locally + mirror to the canonical
        PostgreSQL queue when it is reachable (durable both ways)."""
        item = self._state_machine.enqueue_degraded_mutation(
            module_id=module_id,
            resource_id=resource_id,
            locator_id=locator_id,
            source_revision=source_revision,
            content_hash=content_hash,
            operation=operation,
            embedding_model=self.config.embedding_model,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            payload={"module_id": module_id, **(payload or {})},
        )
        try:
            await self.postgresql.enqueue_reconciliation(item)
        except Exception as exc:
            _logger.warning(
                "CanonicalRagPipeline: PG queue mirror failed for %s:%s: %s",
                module_id, resource_id, exc,
            )

    async def _enqueue_document_mutation(
        self, document: dict[str, Any], chunks: list[dict[str, Any]]
    ) -> None:
        """Queue a durable pending_rag_mutation for a document write."""
        module_id = str(document["module_id"])
        resource_id = str(document["resource_id"])
        content_hash = str(
            document.get("sha256")
            or hashlib.sha256(
                "".join(str(c.get("content") or "") for c in chunks).encode("utf-8")
            ).hexdigest()
        )
        try:
            await self._enqueue_degraded_write(
                module_id=module_id,
                resource_id=resource_id,
                locator_id=str(
                    document.get("locator_id") or f"{module_id}:{resource_id}"
                ),
                source_revision=int(document.get("version") or 1),
                content_hash=content_hash,
                operation="update",
                payload={"title": document.get("title", "")},
            )
        except Exception as exc:
            _logger.warning(
                "CanonicalRagPipeline: mutation enqueue failed for %s:%s: %s",
                module_id, resource_id, exc,
            )

    # -- pending_rag_mutation replay ---------------------------------------

    async def run_reconciliation(self, *, batch: int = 25) -> dict[str, int]:
        """Replay durable pending mutations through the canonical write flow."""
        items = self._queue.lease(limit=batch)
        stats = {
            "leased": len(items), "reconciled": 0,
            "retried": 0, "dead_lettered": 0,
        }
        for item in items:
            try:
                await self._reconcile_item(item)
            except Exception as exc:
                bucket = await self._recon_failure(item, exc)
                stats[bucket] += 1
            else:
                self._queue.mark_verified(item.operation_id)
                await self.postgresql.mark_reconciled(item.operation_id)
                stats["reconciled"] += 1
        self._queue.drain_verified()
        await self.postgresql.drain_reconciled()
        _logger.info("CanonicalRagPipeline: reconciliation run %s", stats)
        return stats

    async def run_parity_sweep(
        self, *, module_id: Optional[str] = None, drain: bool = True
    ) -> dict[str, Any]:
        """§10.6 定期一致性掃描：比對 PG index_state/chunk 與 vectord 逐資源
        point 數、embedding 版本、content hash；只把漂移資源 enqueue 到
        durable reconciliation_queue（預設隨即 drain 修復）。不得以刪除
        vectord collection 作為修復手段。"""
        from .parity_audit import RagParityAudit

        audit = RagParityAudit(self.postgresql, self.vector, self.config)
        report = await audit.sweep(module_id=module_id)
        if drain and report.get("enqueued"):
            report["drain"] = await self.run_reconciliation()
        return report

    _PARITY_SWEEP_INTERVAL_S = float(
        os.environ.get("RAG_PARITY_SWEEP_INTERVAL_S", "300")
    )
    _last_parity_sweep_at: float = 0.0

    async def _maybe_parity_sweep(self) -> Optional[dict[str, Any]]:
        """節流的定期 parity sweep——只在 CANONICAL 且間隔屆滿時執行。"""
        if self._state_machine.state != RagRuntimeState.CANONICAL:
            return None
        now = time.monotonic()
        if now - self._last_parity_sweep_at < self._PARITY_SWEEP_INTERVAL_S:
            return None
        self._last_parity_sweep_at = now
        try:
            return await self.run_parity_sweep()
        except Exception as exc:  # parity sweep 不得中斷 health_check
            _logger.warning("CanonicalRagPipeline: parity sweep failed: %s", exc)
            return {"error": str(exc)}

    async def _recon_failure(self, item: Any, exc: Exception) -> str:
        """Schedule retry or dead-letter; returns the stats bucket name."""
        delay = min(300.0, _RETRY_BASE_SECONDS * (2 ** max(0, item.attempts - 1)))
        result = self._queue.mark_retry(
            item.operation_id, str(exc),
            delay_seconds=delay, max_attempts=_MAX_ATTEMPTS,
        )
        if result == QueueStatus.DEAD_LETTER.value:
            await self.postgresql.dead_letter_reconciliation(
                item.operation_id, str(exc)
            )
            _logger.error(
                "CanonicalRagPipeline: dead-lettered %s:%s (%s): %s",
                item.payload.get("module_id"), item.resource_id,
                item.operation, exc,
            )
            return "dead_lettered"
        await self.postgresql.mark_recon_retry(
            item.operation_id, str(exc),
            datetime.now(timezone.utc).isoformat(),
        )
        return "retried"

    async def _reconcile_item(self, item: Any) -> None:
        """Replay one pending mutation: re-fetch → re-chunk → re-embed →
        vectord upsert/delete → PG index_state → verify."""
        module_id = str(
            item.payload.get("module_id")
            or str(item.locator_id).split(":", 1)[0]
        )
        if item.operation in (
            QueueOperation.TOMBSTONE.value,
            QueueOperation.ARCHIVE.value,
        ):
            await self._reconcile_delete(item, module_id)
            return
        document, chunks, vectors = await self._rebuild_document(
            item, module_id
        )
        # PERF-07: engine-produced f64-le records take the text write —
        # vectord re-derives the projection vector in-engine; float lists
        # keep the legacy vector path (capability fallback).
        if vectors and isinstance(
            vectors[0], (bytes, bytearray, memoryview)
        ):
            written = await self.index_document_text(
                document=document,
                chunks=chunks,
                embedding_records=vectors,
            )
        else:
            written = await self.index_document(
                document=document, chunks=chunks, vectors=vectors
            )
        if not written:
            raise CanonicalCheckError(
                f"canonical rewrite rejected for {module_id}:{item.resource_id}"
            )
        state = await self.postgresql.get_index_state(module_id, item.resource_id)
        if state is None:
            raise CanonicalCheckError(
                f"index_state verification failed for {module_id}:{item.resource_id}"
            )

    async def _reconcile_delete(self, item: Any, module_id: str) -> None:
        """Tombstone/archive replay: remove vectors, raise authoritative tombstone.

        When a ``DeletionCoordinatorRuntime`` is injected
        (``self._deletion_coordinator``) the replay goes through its typed
        runtime entry, which keeps the fixed order (vectord delete before the
        PostgreSQL tombstone) and is idempotent.  Without it the canonical
        pipeline performs the same two steps directly.
        """
        coordinator = getattr(self, "_deletion_coordinator", None)
        if coordinator is not None:
            outcome = await coordinator.reconcile_tombstone_async(
                module_id=module_id,
                resource_id=item.resource_id,
                source_revision=item.source_revision,
                content_hash=item.content_hash,
                reason=f"reconciled-{item.operation}",
            )
            if not outcome.ok:
                raise CanonicalCheckError(
                    "deletion coordinator refused "
                    f"{module_id}:{item.resource_id}: {outcome.reason}"
                )
            return
        await self.vector.delete_resource(module_id, item.resource_id)
        await self.postgresql.raise_tombstone(
            module_id=module_id,
            resource_id=item.resource_id,
            source_revision=item.source_revision,
            content_hash=item.content_hash,
            reason=f"reconciled-{item.operation}",
        )

    async def _rebuild_document(
        self, item: Any, module_id: str
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[Any]]:
        """Re-fetch source content, re-chunk, re-embed (never replay vectors).

        The owning engine is asked for canonical f64-le records first
        (PERF-07); the injected provider stays the capability fallback.
        Either way a fresh embedding is computed — degraded vectors are
        never replayed into the canonical collection.
        """
        if self._document_fetcher is None:
            raise CanonicalCheckError(
                "reconciler requires document_fetcher"
            )
        doc = await self._call_maybe_async(
            self._document_fetcher, module_id, item.locator_id
        )
        content = str((doc or {}).get("content") or "")
        if not content:
            raise CanonicalCheckError(
                f"source content unavailable for {module_id}:{item.resource_id}"
            )
        texts = self._rechunk(content)
        vectors: list[Any] = []
        embed_engine = getattr(self.vector, "embed_texts", None)
        if embed_engine is not None:
            vectors = list(await embed_engine(texts) or [])
        if not vectors:
            if self._embed_texts is None:
                raise CanonicalCheckError(
                    "reconciler requires embed_texts when the engine "
                    "cannot produce canonical embedding records"
                )
            vectors = list(
                await self._call_maybe_async(self._embed_texts, texts) or []
            )
        if len(vectors) != len(texts):
            raise CanonicalCheckError("RAG_EMBEDDING_COUNT_MISMATCH")
        for vector in vectors:
            dim = (
                len(vector) // 8
                if isinstance(vector, (bytes, bytearray, memoryview))
                else len(vector)
            )
            if dim != self.config.embedding_dimension:
                raise CanonicalCheckError(
                    f"EMBEDDING_DIMENSION_MISMATCH: {dim}-dim vector "
                    f"cannot enter the {self.config.embedding_dimension}-dim "
                    "canonical collection — degraded vectors are never replayed"
                )
        return self._reconcile_records(item, module_id, doc, texts), \
            self._reconcile_chunks(item, module_id, doc, texts), vectors

    # -- record builders ----------------------------------------------------

    def _reconcile_records(
        self, item: Any, module_id: str, doc: dict[str, Any], texts: list[str]
    ) -> dict[str, Any]:
        return {
            "module_id": module_id,
            "resource_id": item.resource_id,
            "document_id": doc.get("document_id") or item.resource_id,
            "locator_id": item.locator_id,
            "owner_id": module_id,
            "platform_id": "",
            "data_category": "knowledge",
            "resource_type": "document",
            "resource_label": doc.get("title") or item.resource_id,
            "classification": "shared",
            "title": doc.get("title", ""),
            "source": doc.get("source", ""),
            "sha256": item.content_hash
            or hashlib.sha256("".join(texts).encode("utf-8")).hexdigest(),
            "version": item.source_revision,
            "character_count": sum(len(t) for t in texts),
            "chunk_count": len(texts),
            "embedding_model": self.config.embedding_model,
        }

    def _reconcile_chunks(
        self, item: Any, module_id: str, doc: dict[str, Any], texts: list[str]
    ) -> list[dict[str, Any]]:
        step = max(1, self.config.chunk_size - self.config.chunk_overlap)
        chunks: list[dict[str, Any]] = []
        for sequence, text in enumerate(texts):
            chunk_id = f"{item.resource_id}-recon-{sequence}"
            point_id = str(uuid.uuid5(_POINT_NAMESPACE, chunk_id))
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "sequence": sequence,
                    "character_start": sequence * step,
                    "character_end": sequence * step + len(text),
                    "content": text,
                    "resource_label": doc.get("title") or item.resource_id,
                    "source": doc.get("source", ""),
                    "title": doc.get("title", ""),
                    "point_id": point_id,
                    "vector_point_id": point_id,
                    "locator_fragment": f"#chunk-{sequence}",
                    "payload": {
                        "resource_id": item.resource_id,
                        "module_id": module_id,
                        "content": text,
                        "content_hash": hashlib.sha256(
                            text.encode("utf-8")
                        ).hexdigest(),
                        "reconciled_from": item.operation_id,
                    },
                }
            )
        return chunks

    # -- helpers ------------------------------------------------------------

    def _rechunk(self, content: str) -> list[str]:
        """Canonical chunker contract: fixed window + overlap."""
        size, overlap = self.config.chunk_size, self.config.chunk_overlap
        step = max(1, size - overlap)
        if len(content) <= size:
            return [content]
        return [content[i : i + size] for i in range(0, len(content), step)]

    @staticmethod
    async def _call_maybe_async(func: Any, *args: Any) -> Any:
        if inspect.iscoroutinefunction(func):
            return await func(*args)
        return await asyncio.to_thread(func, *args)

    # -- health surface ------------------------------------------------------

    async def health_check(self) -> dict[str, Any]:
        """Health check for all components (A374)."""
        await self.attempt_recovery()
        state = self._state_machine.state
        is_canonical = state == RagRuntimeState.CANONICAL
        result = {
            "pipeline_ready": self.is_ready(),
            "state": state.value,
            "effective_state": self._state_machine.effective_state,
            "gateway_state": self.gateway_state(),
            "blocked": self._blocked_reason is not None,
            "blocked_reason": self._blocked_reason,
            "reconciliation_failed": self._state_machine.reconciliation_failed,
            "canonical": is_canonical and self._blocked_reason is None,
            "reconciliation_required": self._state_machine.reconciliation_required,
            "queue_pending": self._queue.pending_count(),
            "queue_complete": self._queue.is_complete(),
            "vector": {
                "healthy": self.vector.is_healthy(),
                "collection": self.config.collection_name,
                "loopback": getattr(self.vector, "last_error", None) is None
                or not str(getattr(self.vector, "last_error", "")).startswith(
                    "VECTOR_URL_NOT_LOOPBACK"
                ),
                "collection_error": getattr(self.vector, "collection_error", None),
            },
            "postgresql": {
                "healthy": self.postgresql.is_healthy(),
            },
            "domain_model": "ok",
            "degraded_pipeline_active": self._degraded_pipeline is not None,
            "reconciler_configured": (
                self._document_fetcher is not None
                and self._embed_texts is not None
            ),
        }
        if self._degraded_pipeline is not None:
            degraded_health = await self._degraded_pipeline.health_check()
            result["degraded_pipeline"] = degraded_health

        parity = await self._maybe_parity_sweep()
        if parity is not None:
            result["parity_sweep"] = {
                k: parity.get(k)
                for k in ("checked", "drifted", "enqueued", "unverifiable", "error")
                if parity.get(k) is not None
            }

        # Phase-2 unified status surface
        result["active_generation"] = getattr(self, "_active_generation", None)
        result["canonical_vector_database"] = "vectord"
        result["embedding_model"] = self.config.embedding_model
        result["embedding_dimension"] = self.config.embedding_dimension
        outbox_stats = getattr(self.postgresql, "outbox_stats", None)
        result["outbox"] = (
            await outbox_stats() if outbox_stats is not None
            else {"pending": self._queue.pending_count()}
        )
        recon_status = getattr(self.postgresql, "reconciliation_status", None)
        result["reconciliation"] = (
            await recon_status() if recon_status is not None else {
                "required": self._state_machine.reconciliation_required,
                "pending": self._queue.pending_count(),
            }
        )
        result["degraded_backend"] = {
            "enabled": True,
            "active": self._degraded_pipeline is not None,
            "canonical": False,
        }
        # P4 adaptive plane 生產者：vector_backlog / degraded / degraded_seconds。
        # 欄位級合併、失敗靜默——量測只是提示，不得影響健康檢查主流程。
        try:
            from shared_layer.adaptive import LoadSignals, get_plane

            is_degraded = state == RagRuntimeState.DEGRADED
            get_plane().observe_merge(
                LoadSignals(
                    vector_backlog=int(
                        (result["outbox"] or {}).get("pending") or 0
                    ),
                    degraded=is_degraded,
                    degraded_seconds=(
                        self._state_machine.seconds_in_state
                        if is_degraded
                        else 0.0
                    ),
                ),
                fields=("vector_backlog", "degraded", "degraded_seconds"),
            )
        except Exception:
            pass
        return result

    # ------------------------------------------------------------------
    # RAG-16D: disaster-recovery rebuild — vectord is rebuildable from
    # PostgreSQL authority + qwen3-embedding:4b; SQLite is never the
    # restore source.
    # ------------------------------------------------------------------

    async def rebuild_canonical(
        self,
        generation_manager: Any,
        *,
        module_id: Optional[str] = None,
        batch: int = 50,
        benchmark_fn: Any = None,
    ) -> Any:
        """Rebuild the canonical semantic index from zero.

        Sequence (lifecycle.dr.REBUILD_SEQUENCE):
        START_VECTORD -> CREATE_GENERATION -> READ_PG_METADATA ->
        RESOLVE_SOURCES -> RECHUNK_REEMBED -> VALIDATE -> ACTIVATE.

        Sources come from PostgreSQL (chunk rows carry canonical content);
        resources missing PG chunks are resolved through the owning
        module's ``_document_fetcher`` and re-chunked — never from the
        degraded SQLite store (its 256-dim hashing vectors cannot enter
        the 2560-dim canonical collection).
        """
        from .lifecycle.dr import RebuildStep, evaluate_rebuild

        steps: list[RebuildStep] = []
        rebuilt = 0
        self._migrating = True
        try:
            # START_VECTORD
            if not await self.vector.initialize() and not self.vector.is_healthy():
                return evaluate_rebuild(tuple(steps), 0, 0)
            steps.append(RebuildStep.START_VECTORD)

            # CREATE_GENERATION (BUILDING — never serves queries)
            generation = await generation_manager.create_generation()
            await generation_manager.ensure_collection(generation)
            steps.append(RebuildStep.CREATE_GENERATION)

            # READ_PG_METADATA — PostgreSQL is the rebuild authority
            list_fn = getattr(self.postgresql, "list_indexed_resources", None)
            resources = (
                await list_fn(module_id) if list_fn is not None else []
            )
            steps.append(RebuildStep.READ_PG_METADATA)

            # RESOLVE_SOURCES + RECHUNK_REEMBED — B61/C56: chunks that
            # carry a canonical embedding are projected as a copy; only
            # rows missing one are re-embedded and backfilled into PG.
            for mid, rid in resources:
                chunks = await self.postgresql.fetch_resource_chunks(mid, rid)
                texts = [str(c.get("content") or "") for c in chunks]
                if (
                    not any(texts)
                    and not all(c.get("embedding") for c in chunks)
                    and self._document_fetcher is not None
                ):
                    doc = await self._call_maybe_async(
                        self._document_fetcher, mid, rid
                    )
                    if doc is None:
                        continue
                    texts = [str(doc.get("content") or "")]
                    chunks = [{
                        "chunk_id": f"{rid}-rebuilt-0",
                        "vector_point_id": None,
                        "sequence": 0,
                        "content": texts[0],
                        "embedding": None,
                        "payload": {},
                    }]
                vectors: list = [c.get("embedding") for c in chunks]
                missing = [i for i, v in enumerate(vectors) if v is None]
                if not chunks or (missing and self._embed_texts is None):
                    continue
                if missing:
                    fresh = await self._call_maybe_async(
                        self._embed_texts,
                        [texts[i] for i in missing],
                    )
                    if len(fresh) != len(missing):
                        continue
                    for i, v in zip(missing, fresh):
                        vectors[i] = v
                    await self.postgresql.store_chunk_embeddings(
                        mid, rid,
                        {chunks[i]["chunk_id"]: vectors[i] for i in missing},
                    )
                for v in vectors:
                    if len(v) != self.config.embedding_dimension:
                        raise CanonicalCheckError(
                            f"EMBEDDING_DIMENSION_MISMATCH:{len(v)}"
                        )
                from .vector_models import PointStruct
                from .canonical_vector_runtime import sanitize_payload
                points = [
                    PointStruct(
                        id=str(
                            c.get("vector_point_id")
                            or uuid.uuid5(_POINT_NAMESPACE, c["chunk_id"])
                        ),
                        vector=[float(x) for x in v],
                        payload=sanitize_payload({
                            "module_id": mid,
                            "document_resource_id": rid,
                            "chunk_id": c["chunk_id"],
                            "generation_id": generation.generation_id,
                        }),
                    )
                    for c, v in zip(chunks, vectors)
                ]
                if not await self.vector.upsert_points(
                    points, generation_id=generation.generation_id
                ):
                    continue
                rebuilt += 1
            steps.append(RebuildStep.RESOLVE_SOURCES)
            steps.append(RebuildStep.RECHUNK_REEMBED)

            # VALIDATE — dimension/verify + optional benchmark gate
            verification = await generation_manager.verify_generation(generation)
            if not verification.get("ok"):
                return evaluate_rebuild(tuple(steps), rebuilt, len(resources))
            if benchmark_fn is not None:
                bench = await self._call_maybe_async(benchmark_fn, generation)
                if not bench:
                    return evaluate_rebuild(tuple(steps), rebuilt, len(resources))
            steps.append(RebuildStep.VALIDATE)

            # ACTIVATE — atomic alias swap, then bind this pipeline
            if not await generation_manager.promote_to_active(generation):
                return evaluate_rebuild(tuple(steps), rebuilt, len(resources))
            self._active_generation = generation.generation_id
            steps.append(RebuildStep.ACTIVATE)
            return evaluate_rebuild(tuple(steps), rebuilt, len(resources))
        finally:
            self._migrating = False

    async def migrate_schema(
        self,
        generation_manager: Any,
        *,
        current: Any,
        target: Any,
        apply_metadata: Any = None,
        validate: Any = None,
        benchmark_fn: Any = None,
    ) -> Any:
        """G50/P2 migration tool entry: schema drift -> plan -> governed
        phase execution.

        Vector-axis drift triggers ``rebuild_canonical`` (new generation
        built from PostgreSQL authority, validated, benchmarked, then
        atomically promoted). The metadata axis requires an injected
        ``apply_metadata`` executor — a plan lacking one is blocked
        before any mutation. Returns ``lifecycle.MigrationReport``.
        """
        from .lifecycle.migration import apply_migration
        from .lifecycle.schema_versions import plan_migration

        plan = plan_migration(current, target)

        async def _build(_plan: Any) -> Any:
            return await self.rebuild_canonical(
                generation_manager, benchmark_fn=benchmark_fn
            )

        return await apply_migration(
            plan,
            apply_metadata=apply_metadata,
            build_vector_generation=_build,
            validate=validate,
        )

    # ------------------------------------------------------------------
    # RAG-16: unified status / manifest surface
    # ------------------------------------------------------------------

    def manifest(self) -> Any:
        """RagSystemManifest for this runtime — the machine-readable
        answer to "which architecture produced this answer?"."""
        from .lifecycle.manifest import RagSystemManifest
        from .lifecycle.schema_versions import RagSchemaVersions

        return RagSystemManifest(
            architecture_version="v1",
            policy_version="v1",
            schema=RagSchemaVersions(
                rag_schema_version=1,
                metadata_schema_version=1,
                vector_schema_version=1,
            ),
            active_generation=self._active_generation or "",
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            collection_alias=self.config.collection_name,
            canonical_state=self.gateway_state(),
            degraded_backend="pg-bounded-degraded",
        )

    async def status(self) -> dict[str, Any]:
        """Unified status surface: manifest core + health + metrics."""
        health = await self.health_check()
        metrics = getattr(self, "_metrics", None)
        from .observability import RAG_METRICS
        metrics = metrics or RAG_METRICS
        # Feed gauges from the live stores.
        outbox = health.get("outbox") or {}
        metrics.set_gauge("outbox_pending", int(outbox.get("pending") or 0))
        metrics.set_gauge("outbox_retry", int(outbox.get("retry") or 0))
        metrics.set_gauge(
            "outbox_dead_letter", int(outbox.get("dead_letter") or 0)
        )
        recon = health.get("reconciliation") or {}
        metrics.set_gauge(
            "reconciliation_pending",
            int(recon.get("pending_count") or recon.get("pending") or 0),
        )
        metrics.set_gauge(
            "reconciliation_failed", int(recon.get("failed_count") or 0)
        )
        metrics.set_gauge("active_generation", self._active_generation or "")
        return {
            "state": self.gateway_state(),
            "canonical": self.gateway_state() == "CANONICAL_READY",
            "blocked_reason": self._blocked_reason,
            "manifest": {
                "architecture_version": self.manifest().architecture_version,
                "active_generation": self._active_generation or "",
                "embedding_model": self.config.embedding_model,
                "embedding_dimension": self.config.embedding_dimension,
                "collection_alias": self.config.collection_name,
                "sub_architectures": list(self.manifest().sub_architectures),
            },
            "health": health,
            "metrics": metrics.snapshot(),
        }


class CanonicalRagPipeline(
    PipelineRetrievalMixin,
    PipelineRecoveryMixin,
    PipelineOutboxMixin,
    PipelineMaintenanceMixin,
    PipelineDocumentsMixin,
):
    """A371-A374: canonical path — vectord dense retrieval > PostgreSQL
    metadata/FTS/index_state authority > domain model > typed result.
    A373: normal execution must prove vectord + PostgreSQL are live."""

    def __init__(
        self,
        config: RagPipelineConfig,
        *,
        document_fetcher: Optional[Any] = None,
        embed_texts: Optional[Any] = None,
        vector_runtime: Optional[Any] = None,
    ) -> None:
        self.config = config
        # A610 target-primary: the canonical vector runtime is the Rust
        # vectord engine by default; ``vector_runtime`` stays injectable
        # for governed stand-ins and migration tooling.
        self.vector = vector_runtime or select_vector_runtime(config)
        self.postgresql = PostgreSQLMetadataAuthority(config.postgresql_dsn)
        self.domain_model = PythonDomainModel(config)
        self._initialized = False
        self._degraded_pipeline: Optional[DegradedRagPipeline] = None
        # A374 reconciliation data flow: the fetcher re-reads the owning
        # module's original content and the embedder re-embeds through the
        # governed local runtime (qwen3-embedding:4b / 2560d).  Degraded
        # vectors are never replayed into the canonical collection.
        self._document_fetcher = document_fetcher
        self._embed_texts = embed_texts
        # A374: runtime state machine.  The durable queue lives in the
        # ``gptbridge_rag`` PostgreSQL schema (A610/A621: SQLite retired).
        # The TombstoneGuard is the in-memory authoritative view;
        # PostgreSQL is the durable tombstone store.
        self._queue_db = pg_connect(self.config.queue_schema, autocommit=False)
        self._queue = ReconciliationQueue(self._queue_db)
        self._tombstone = TombstoneGuard()
        self._outbox = CrossStoreOutbox(self._queue, self._tombstone)
        self._state_machine = RagRuntimeStateMachine(
            self._queue, self._tombstone, self._outbox
        )
        # Canonical takeover: sticky hard-contract violation (dimension
        # mismatch, non-loopback vectord URL, unverifiable contract).  While
        # set the surface state is BLOCKED and canonical reads/writes raise
        # instead of silently delegating to the degraded backend.
        self._blocked_reason: Optional[str] = None
        # RAG-11: set while a BUILDING generation / alias migration runs;
        # surfaces as MIGRATING on the gateway state.
        self._migrating = False
        # RAG-16: ACTIVE generation id bound to this pipeline; retrieval
        # drops hits carrying a different generation_id.
        self._active_generation: Optional[str] = None
        # G50: production assembly point for the A486/A487 generation
        # lifecycle.  Constructed during initialize() once vectord +
        # PostgreSQL are proven; ``_generation_rebuild_required`` flags a
        # config-fingerprint drift that demands a NEW generation (never an
        # in-place edit of the ACTIVE one).
        self.generation_manager: Optional[Any] = None
        self._generation_rebuild_required = False

    @property
    def state(self) -> RagRuntimeState:
        """A374: current runtime state."""
        return self._state_machine.state

    @property
    def state_machine(self) -> RagRuntimeStateMachine:
        return self._state_machine

    def _get_degraded_pipeline(self) -> DegradedRagPipeline:
        """Lazily initialize and return the degraded pipeline."""
        if self._degraded_pipeline is None:
            root = Path(self.config.degraded_root) if self.config.degraded_root else None
            self._degraded_pipeline = DegradedRagPipeline(
                self.config,
                degraded_root=root,
                enqueue_mutation=self._enqueue_degraded_write,
            )
        return self._degraded_pipeline

    async def initialize(self) -> bool:
        """Initialize all canonical components in order (A374).

        After initialization, evaluate the A374 startup readiness gate:
        STARTING -> CANONICAL only when vectord + PostgreSQL are healthy,
        the authoritative index_state matches, and the reconciliation queue
        is complete.  Otherwise STARTING -> DEGRADED.
        """
        _logger.info("CanonicalRagPipeline: initializing...")

        # Step 1: vectord canonical runtime
        vector_ok = await self.vector.initialize()
        if vector_ok:
            await self.vector.ensure_collection()
        if self.vector.collection_error or "_URL_NOT_LOOPBACK" in (
            self.vector.last_error or ""
        ):
            # Hard contract violation — BLOCKED, never silently degraded.
            self._blocked_reason = (
                self.vector.collection_error or self.vector.last_error
            )

        # Step 2: PostgreSQL metadata authority
        pg_ok = await self.postgresql.initialize()

        # G50: bind the ACTIVE index generation once both stores are
        # proven — retrieval drops hits from any other generation
        # (RAG-16), and a config-fingerprint drift marks the pipeline
        # as needing a new-generation build before canonical indexing.
        vector_client = getattr(self.vector, "client", None)
        if vector_ok and pg_ok and vector_client is not None:
            try:
                from .generation import GenerationConfig, GenerationManager

                self.generation_manager = GenerationManager(
                    vector_client,
                    GenerationConfig(
                        alias_name=self.config.collection_name,
                        embedding_model=self.config.embedding_model,
                        embedding_dimension=self.config.embedding_dimension,
                        chunk_size=self.config.chunk_size,
                        chunk_overlap=self.config.chunk_overlap,
                    ),
                    self.postgresql,
                )
                active = await self.generation_manager.get_active_generation()
                if active is not None:
                    self._active_generation = str(active.generation_id)
                    self._generation_rebuild_required = (
                        await self.generation_manager.requires_rebuild(active)
                    )
                    if self._generation_rebuild_required:
                        _logger.warning(
                            "CanonicalRagPipeline: active generation %s "
                            "fingerprint drifted from config — new "
                            "generation build required",
                            self._active_generation,
                        )
            except Exception as exc:
                _logger.warning(
                    "CanonicalRagPipeline: generation binding failed: %s", exc
                )

        self._initialized = (
            vector_ok and pg_ok and self._blocked_reason is None
        )
        _logger.info("CanonicalRagPipeline: initialized=%s (vector=%s, pg=%s)", self._initialized, vector_ok, pg_ok)

        # A374: evaluate startup readiness gate.
        index_state_matches = self._initialized  # minimal: both stores up
        if self._generation_rebuild_required:
            # The ACTIVE generation's fingerprint no longer matches the
            # configured embedding/index contract — canonical indexing is
            # stale until a new generation is built/verified/activated.
            index_state_matches = False
        if pg_ok:
            # If PostgreSQL is healthy, mirror any pending canonical queue
            # items into the local queue view so the startup gate can see
            # whether reconciliation is still required.
            try:
                pg_pending = await self.postgresql.pending_reconciliation_count()
                if pg_pending > 0:
                    index_state_matches = False
            except Exception as exc:
                _logger.warning("CanonicalRagPipeline: queue check failed: %s", exc)
                index_state_matches = False
        self._state_machine.evaluate_startup(
            vector_healthy=vector_ok,
            postgresql_healthy=pg_ok,
            index_state_matches=index_state_matches,
        )
        return self._initialized

    @property
    def blocked_reason(self) -> Optional[str]:
        return self._blocked_reason

    def gateway_state(self) -> str:
        """Canonical takeover surface state (BOOTSTRAPPING/CANONICAL_READY/
        DEGRADED_READY/RECONCILING/BLOCKED).  The internal A374 machine keeps
        exactly four states; this maps them to the governed surface names."""
        if self._blocked_reason:
            return "BLOCKED"
        if self._migrating:
            return "MIGRATING"
        mapping = {
            "STARTING": "BOOTSTRAPPING",
            "CANONICAL": "CANONICAL_READY",
            "DEGRADED": "DEGRADED_READY",
            "RECONCILING": "RECONCILING",
            "RECONCILIATION_FAILED": "DEGRADED_READY",
        }
        return mapping.get(self._state_machine.effective_state, "BOOTSTRAPPING")

    def is_ready(self) -> bool:
        """A373: Prove vectord + PostgreSQL are live path."""
        return (
            self._initialized
            and self._blocked_reason is None
            and self.vector.is_healthy()
            and self.postgresql.is_healthy()
        )

    async def index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> IndexState:
        """Index a resource through the canonical or degraded path depending on state."""
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        await self.attempt_recovery()
        current_state = self.state
        if current_state == RagRuntimeState.DEGRADED:
            _logger.info("CanonicalRagPipeline: DEGRADED state, delegating index_resource to degraded pipeline")
            return await self._get_degraded_pipeline().index_resource(module_id, resource_id, content, metadata, embedding)
        if current_state != RagRuntimeState.CANONICAL:
            raise RuntimeError(f"RAG pipeline not ready for indexing (state={current_state.value})")

        # Canonical path
        try:
            return await self._canonical_index_resource(
                module_id, resource_id, content, metadata, embedding
            )
        except Exception as exc:
            _logger.error("CanonicalRagPipeline: canonical index_resource failed: %s", exc)
            self.state_machine.report_canonical_failure(str(exc))
            return await self._get_degraded_pipeline().index_resource(module_id, resource_id, content, metadata, embedding)

    async def _canonical_index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> IndexState:
        """Canonical index_resource: vectord write, then index_state writeback."""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        point_id = str(uuid.uuid4())
        now_utc = datetime.now(timezone.utc).isoformat()
        # Payload contract: opaque ids + filterable metadata only — content
        # and physical locators live in PostgreSQL, never in vectord.
        point = PointStruct(
            id=point_id,
            vector=embedding,
            payload=sanitize_payload(
                {
                    "resource_id": resource_id,
                    "module_id": module_id,
                    "content_hash": content_hash,
                    "indexed_at_utc": now_utc,
                    **metadata,
                }
            ),
        )
        await self.vector.upsert_points([point])
        index_state = IndexState(
            resource_id=resource_id,
            module_id=module_id,
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            indexed_at_utc=now_utc,
            content_hash=content_hash,
            vector_point_id=point_id,
            source_revision=int(
                metadata.get("version") or metadata.get("source_version") or 1
            ),
        )
        await self.postgresql.upsert_index_state(index_state)
        return index_state

    async def query(
        self,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> list[RagQueryResult]:
        """Query through the canonical or degraded RAG path depending on state."""
        if not str(module_id or "").strip():
            raise VectorScopeError("VECTOR_MODULE_SCOPE_REQUIRED")
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        await self.attempt_recovery()
        current_state = self.state
        if current_state == RagRuntimeState.DEGRADED:
            _logger.info("CanonicalRagPipeline: DEGRADED state, delegating query to degraded pipeline")
            return await self._get_degraded_pipeline().query(query_embedding, module_id, top_k, score_threshold)
        if current_state != RagRuntimeState.CANONICAL:
            raise RuntimeError(f"RAG pipeline not ready for query (state={current_state.value})")

        # Canonical path
        try:
            # A373: CANONICAL-TAKEOVER - prove vectord + PostgreSQL are live
            if not self.vector.is_healthy():
                raise RuntimeError("vectord canonical runtime not healthy")
            if not self.postgresql.is_healthy():
                raise RuntimeError("PostgreSQL metadata authority not healthy")

            # Step 1: vectord dense retrieval
            vector_hits = await self.vector.search(
                query_vector=query_embedding,
                module_id=module_id,
                top_k=top_k,
                score_threshold=score_threshold,
            )

            if not vector_hits:
                return []

            pg_metadata, index_states = await self._pg_evidence(vector_hits)
            pg_chunks = await self.postgresql.fetch_chunks_for_points(
                (module_id,) if module_id else tuple(
                    str(h.get("payload", {}).get("module_id") or "")
                    for h in vector_hits
                ),
                [str(h.get("id")) for h in vector_hits],
            )

            # Step 4: Build typed results via Python domain model
            return self.domain_model.build_typed_result(
                vector_hits, pg_metadata, index_states, pg_chunks=pg_chunks
            )
        except Exception as exc:
            _logger.error("CanonicalRagPipeline: canonical query failed: %s", exc)
            # Transition to degraded mode
            self.state_machine.report_canonical_failure(str(exc))
            # Retry with degraded pipeline
            return await self._get_degraded_pipeline().query(query_embedding, module_id, top_k, score_threshold)

    async def _pg_evidence(
        self, vector_hits: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Steps 2-3: batch-fetch PostgreSQL metadata + index_state proof."""
        resource_ids = [
            hit.get("payload", {}).get("resource_id") or hit.get("id")
            for hit in vector_hits
        ]
        module_ids = set(
            hit.get("payload", {}).get("module_id")
            for hit in vector_hits
            if hit.get("payload", {}).get("module_id")
        )
        pg_metadata: dict[str, Any] = {}
        index_states: dict[str, Any] = {}
        get_states = getattr(self.postgresql, "get_index_states", None)
        for mid in module_ids:
            pg_metadata.update(await self.postgresql.fetch_metadata(mid, resource_ids))
            if get_states is not None:
                # P15: one round trip per module instead of one per resource.
                for rid, state in (
                    await get_states(mid, resource_ids)
                ).items():
                    index_states[f"{mid}:{rid}"] = state
            else:
                for rid in resource_ids:
                    state = await self.postgresql.get_index_state(mid, rid)
                    if state:
                        index_states[f"{mid}:{rid}"] = state
        return pg_metadata, index_states

    async def get_index_state(self, module_id: str, resource_id: str) -> Optional[IndexState]:
        """Get authoritative index state (A374)."""
        return await self.postgresql.get_index_state(module_id, resource_id)

    async def vector_search(
        self,
        query_embedding: list[float],
        *,
        module_ids: tuple[str, ...],
        top_k: int,
        score_threshold: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Canonical dense retrieval with index_state proof (A373/A374).

        Returns payload-shaped records (chunk-level) whose document carries an
        authoritative index_state row; hits without proof are dropped.
        """
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        await self.attempt_recovery()
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        from .observability import RAG_METRICS, current_trace, timed_stage
        RAG_METRICS.inc("rag_query_total")
        RAG_METRICS.inc("canonical_query_total")
        trace = current_trace()
        started = time.perf_counter()
        try:
            with timed_stage("vector"):
                hits = await self.vector.search(
                    query_vector=query_embedding,
                    module_ids=module_ids,
                    top_k=top_k,
                    score_threshold=score_threshold,
                )
        except Exception:
            RAG_METRICS.inc("rag_query_failed_total")
            RAG_METRICS.inc("vector_error_total")
            RAG_METRICS.observe_latency(
                (time.perf_counter() - started) * 1000
            )
            raise
        return await self._prove_vector_hits(hits, module_ids, trace, started)

    async def text_search(
        self,
        query_text: str,
        *,
        module_ids: tuple[str, ...],
        top_k: int,
        score_threshold: Optional[float] = None,
    ) -> list[dict[str, Any]]:
        """Canonical dense retrieval by raw text (PERF-07).

        The owning engine embeds ``query_text`` internally — no vector is
        serialised across the Python↔vectord boundary.  The PostgreSQL
        index_state proof barrier below is identical to
        :meth:`vector_search`.
        """
        if self._blocked_reason:
            raise RuntimeError(self._blocked_reason)
        await self.attempt_recovery()
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        from .observability import RAG_METRICS, current_trace, timed_stage
        RAG_METRICS.inc("rag_query_total")
        RAG_METRICS.inc("canonical_query_total")
        trace = current_trace()
        started = time.perf_counter()
        try:
            with timed_stage("vector"):
                hits = await self.vector.search_text(
                    query_text,
                    module_ids=module_ids,
                    top_k=top_k,
                    score_threshold=score_threshold,
                )
        except Exception:
            RAG_METRICS.inc("rag_query_failed_total")
            RAG_METRICS.inc("vector_error_total")
            RAG_METRICS.observe_latency(
                (time.perf_counter() - started) * 1000
            )
            raise
        return await self._prove_vector_hits(hits, module_ids, trace, started)

    async def _prove_vector_hits(
        self,
        hits: list[dict[str, Any]],
        module_ids: tuple[str, ...],
        trace: Any,
        started: float,
    ) -> list[dict[str, Any]]:
        """Shared canonical read barrier for vector/text searches."""
        from .observability import RAG_METRICS, timed_stage
        if not hits:
            RAG_METRICS.inc("retrieval_zero_result_total")
            RAG_METRICS.observe_latency(
                (time.perf_counter() - started) * 1000
            )
            return []
        # Canonical read barrier: one batch PG lookup proves every hit —
        # chunk metadata exists, resource is not tombstoned, module scope is
        # in the governed request scope — and hydrates content from the PG
        # authority (vectord payloads never carry content).
        with timed_stage("postgres_fts"):
            chunk_rows = await self.postgresql.fetch_chunks_for_points(
                tuple(module_ids), [str(hit.get("id")) for hit in hits]
            )
        scope = {str(mid) for mid in module_ids}
        # Batch the index-state proof: one PostgreSQL round trip per module
        # instead of one per hit (P15 N+1 remediation).
        rids_by_module: dict[str, list[str]] = {}
        for hit in hits:
            payload = hit.get("payload") or {}
            mid = str(payload.get("module_id") or "")
            rid = str(
                payload.get("document_resource_id") or payload.get("resource_id") or ""
            )
            if mid and rid:
                rids_by_module.setdefault(mid, []).append(rid)
        index_states: dict[tuple[str, str], Any] = {}
        get_states = getattr(self.postgresql, "get_index_states", None)
        for mid, rids in rids_by_module.items():
            if get_states is not None:
                for rid, state in (await get_states(mid, rids)).items():
                    index_states[(mid, str(rid))] = state
            else:
                for rid in rids:
                    state = await self.postgresql.get_index_state(mid, rid)
                    if state is not None:
                        index_states[(mid, rid)] = state
        proved: list[dict[str, Any]] = []
        for hit in hits:
            payload = hit.get("payload") or {}
            module_id = str(payload.get("module_id") or "")
            document_resource_id = str(
                payload.get("document_resource_id") or payload.get("resource_id") or ""
            )
            if not module_id or module_id not in scope or not document_resource_id:
                if trace is not None:
                    trace.drop("unauthorized")
                RAG_METRICS.inc("unauthorized_hit_dropped_total")
                continue
            if (
                self._active_generation
                and str(payload.get("generation_id") or "")
                not in ("", self._active_generation)
            ):
                if trace is not None:
                    trace.drop("generation_mismatch")
                RAG_METRICS.inc("generation_mismatch_hit_dropped_total")
                continue
            row = chunk_rows.get(str(hit.get("id")))
            if row is None:
                # no canonical chunk metadata / tombstoned
                if trace is not None:
                    trace.drop("missing_metadata")
                RAG_METRICS.inc("missing_metadata_hit_dropped_total")
                continue
            if str(row.get("resource_id")) != document_resource_id:
                if trace is not None:
                    trace.drop("missing_metadata")
                RAG_METRICS.inc("missing_metadata_hit_dropped_total")
                continue  # canonical metadata conflicts with the vector hit
            state = index_states.get((module_id, document_resource_id))
            if state is None or str(state.status).lower() not in ("indexed", "active"):
                if trace is not None:
                    trace.drop("tombstoned")
                RAG_METRICS.inc("tombstoned_hit_dropped_total")
                continue
            record = {
                **payload,
                **{key: value for key, value in row.items() if key != "point_id"},
                "id": str(hit.get("id")),
                "point_id": str(hit.get("id")),
                "module_id": module_id,
                "vector_score": round(float(hit.get("score") or 0.0), 6),
                "score": round(float(hit.get("score") or 0.0), 6),
                "index_state": state,
            }
            proved.append(record)
        RAG_METRICS.inc("rag_query_success_total")
        RAG_METRICS.observe_latency((time.perf_counter() - started) * 1000)
        if trace is not None:
            trace.hit_count = len(proved)
        return proved

    async def keyword_search(
        self,
        query: str,
        *,
        module_ids: tuple[str, ...],
        limit: int,
    ) -> list[dict[str, Any]]:
        """Canonical PostgreSQL FTS keyword channel (A374 step 2)."""
        await self.attempt_recovery()
        if not self.is_ready():
            raise RuntimeError("RAG pipeline not ready")
        from .observability import timed_stage
        with timed_stage("postgres_fts"):
            return await self.postgresql.keyword_search(
                query, module_ids=module_ids, limit=limit
            )

    # ------------------------------------------------------------------
    # A52: Four sub-architecture retrievers — share vectord + PostgreSQL +
    # embedding runtime + reranker + governance, but differ in retrieval
    # behavior.  The reranker is injected (A49: pipeline does not own a
    # model load).
    # ------------------------------------------------------------------

    def hybrid_retriever(self, reranker=None):
        """A52 hybrid-rag: vectord Dense + PG FTS + RRF."""
        from .retrievers import HybridRetriever
        return HybridRetriever(self, reranker)

    def code_retriever(self, reranker=None):
        """A52 code-rag: chunk + AST + symbol + dependency."""
        from .retrievers import CodeRetriever
        return CodeRetriever(self, reranker)

    def agentic_retriever(self, reranker=None, reformulator=None):
        """A52 agentic-rag: retrieve → evaluate → reformulate → retrieve."""
        from .retrievers import AgenticRetriever
        return AgenticRetriever(self, reranker, reformulator)

    def memory_retriever(self, reranker=None):
        """A52 memory-rag: session + episodic + long-term."""
        from .retrievers import MemoryRetriever
        return MemoryRetriever(self, reranker)


def create_rag_pipeline_from_env() -> CanonicalRagPipeline:
    """Create pipeline from environment variables."""
    config = RagPipelineConfig(
        collection_name=os.environ.get(
            "VECTOR_COLLECTION", "gptbridge_shared_knowledge"
        ),
        postgresql_dsn=os.environ.get("POSTGRESQL_DSN", ""),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "qwen3-embedding:4b"),
        embedding_dimension=int(os.environ.get("EMBEDDING_DIMENSION", "2560")),
        embedding_provider=os.environ.get("EMBEDDING_PROVIDER", "ollama"),
        chunk_size=int(os.environ.get("CHUNK_SIZE", "1200")),
        chunk_overlap=int(os.environ.get("CHUNK_OVERLAP", "200")),
    )
    return CanonicalRagPipeline(config)



def _default_degraded_root() -> Path:
    """Return a default root for degraded stores outside the project tree."""
    return Path(tempfile.gettempdir()) / "gptbridge_degraded_rag"


class DegradedRagPipeline:
    """A44 Degraded Mode RAG pipeline using local stores.

    This pipeline is used when canonical vectord/PostgreSQL are unavailable.
    All operations are non-canonical and require reconciliation.
    """

    def __init__(
        self,
        config: RagPipelineConfig,
        degraded_root: Optional[Path] = None,
        enqueue_mutation: Optional[Any] = None,
        degraded_schema: Optional[str] = None,
    ) -> None:
        self.config = config
        # A374: every degraded mutation must be recorded as a durable
        # pending_rag_mutation so recovery can replay it canonically.
        self._enqueue_mutation = enqueue_mutation

        if degraded_root is None:
            degraded_root = _default_degraded_root()
        degraded_root.mkdir(parents=True, exist_ok=True)

        store_kwargs: dict[str, Any] = (
            {"schema": degraded_schema} if degraded_schema else {}
        )
        self.vector_store = LocalVectorStore(
            degraded_root, dimension=config.embedding_dimension, **store_kwargs
        )
        self.repository = LocalRagRepository(degraded_root, **store_kwargs)
        self.vector_store.ensure_collection(config.embedding_dimension)

        _logger.info("DegradedRagPipeline: initialized at %s", degraded_root)

    def is_ready(self) -> bool:
        """Degraded pipeline is always ready (local stores)."""
        return True

    async def index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> IndexState:
        """Index a resource through the degraded path."""
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        point_id = str(uuid.uuid4())
        now_utc = datetime.now(timezone.utc).isoformat()

        self._mirror_document(
            module_id, resource_id, content, metadata, content_hash,
            point_id, embedding, now_utc,
        )
        if self._enqueue_mutation is not None:
            await self._enqueue_mutation(
                module_id=module_id,
                resource_id=resource_id,
                locator_id=f"{module_id}:{resource_id}",
                source_revision=int(metadata.get("version") or 1),
                content_hash=content_hash,
                operation="update",
                payload={"title": metadata.get("title", "")},
            )
        return IndexState(
            resource_id=resource_id,
            module_id=module_id,
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            indexed_at_utc=now_utc,
            content_hash=content_hash,
            vector_point_id=point_id,
        )

    def _mirror_document(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        content_hash: str,
        point_id: str,
        embedding: list[float],
        now_utc: str,
    ) -> None:
        """Write the bounded degraded mirror (vector cache + local repo)."""
        point = {
            "id": point_id,
            "vector": embedding,
            "payload": {
                "resource_id": resource_id,
                "module_id": module_id,
                "content": content,
                "content_hash": content_hash,
                "indexed_at_utc": now_utc,
                **metadata,
            },
            "module_id": module_id,
        }
        self.vector_store.replace_document(resource_id, [point], module_id=module_id)
        self.repository.replace_document(
            document=self._document_record(module_id, resource_id, content, metadata, content_hash),
            chunks=[{
                "chunk_id": point_id,
                "sequence": 0,
                "character_start": 0,
                "character_end": len(content),
                "point_id": point_id,
                "resource_label": resource_id,
                "content": content,
            }],
        )

    def _document_record(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        content_hash: str,
    ) -> dict[str, Any]:
        """Repository document record for the degraded path."""
        return {
            "resource_id": resource_id,
            "module_id": module_id,
            "document_id": resource_id,
            "source": content,
            "title": metadata.get("title", ""),
            "character_count": len(content),
            "chunk_count": 1,
            "embedding_model": self.config.embedding_model,
            "owner_id": module_id,
            "platform_id": "",
            "data_category": "degraded",
            "resource_type": "document",
            "resource_label": resource_id,
            "classification": "degraded",
            "locator_id": f"{module_id}:{resource_id}",
            "sha256": content_hash,
            "version": 1,
        }

    async def query(
        self,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> list[RagQueryResult]:
        """Query through the degraded path."""
        module_ids = (module_id,) if module_id else ()
        hits = self.vector_store.query(
            query_embedding,
            limit=top_k or self.config.top_k,
            module_ids=module_ids,
        )
        if score_threshold is not None:
            hits = [h for h in hits if h.get("score", 0) >= score_threshold]
        return [r for hit in hits if (r := self._result(hit)) is not None]

    def _result(self, hit: dict[str, Any]) -> Optional[RagQueryResult]:
        """Build a typed result from a local vector hit (no PG metadata).

        ``LocalVectorStore.query`` returns flat rows — the stored payload
        fields are promoted to the top level alongside ``document_id``,
        ``point_id``, ``vector_score`` and ``score``.
        """
        resource_id = hit.get("resource_id") or hit.get("document_id")
        module_id = hit.get("module_id")
        if not resource_id or not module_id:
            return None
        return RagQueryResult(
            resource_id=resource_id,
            module_id=module_id,
            content=hit.get("content", ""),
            score=hit.get("score", 0.0),
            metadata=hit,
            index_state=IndexState(
                resource_id=resource_id,
                module_id=module_id,
                embedding_model=self.config.embedding_model,
                embedding_dimension=self.config.embedding_dimension,
                chunk_size=self.config.chunk_size,
                chunk_overlap=self.config.chunk_overlap,
                indexed_at_utc=hit.get("indexed_at_utc", ""),
                content_hash=hit.get("content_hash", ""),
                vector_point_id=hit.get("point_id", ""),
            ),
        )

    async def health_check(self) -> dict[str, Any]:
        """Health check for degraded pipeline."""
        return {
            "pipeline_ready": True,
            "degraded": True,
            "canonical": False,
            "reconciliation_required": True,
            "vector_store": self.vector_store.status(),
            "repository": self.repository.status(),
        }


__all__ = [
    # Canonical pipeline
    "CanonicalRagPipeline",
    "RagPipelineConfig",
    "IndexState",
    "RagQueryResult",
    "CanonicalVectorRuntime",
    "PostgreSQLMetadataAuthority",
    "DegradedRagPipeline",
    "create_rag_pipeline_from_env",
    # Domain model
    "PythonDomainModel",
    # Retrieval (A52)
    "PipelineRetrievalMixin",
    "RerankerFn",
    "reciprocal_rank_fusion",
    # Mixins composing CanonicalRagPipeline
    "PipelineOutboxMixin",
    "PipelineMaintenanceMixin",
    "PipelineDocumentsMixin",
    "PipelineRecoveryMixin",
]
