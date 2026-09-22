"""RAG-16 Observability — unified request trace + metrics registry.

One trace per request: ``rag_request_id`` flows admission → scope →
planning → embedding → Qdrant → PostgreSQL FTS → fusion → reranker →
context build → generation → citation validation → response.

Traces carry timings and counts only — never full document content or
prompt text.  ``RagMetrics`` is the programmatic snapshot surface
(``snapshot()``); logs are not the metrics API.
"""
from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

# Request-scoped trace handle (separate from rag_request_id_var which
# carries only the id string).
_current_trace: contextvars.ContextVar[Optional["RagTrace"]] = (
    contextvars.ContextVar("rag_current_trace", default=None)
)

TRACE_STAGES: tuple[str, ...] = (
    "admission",
    "authorization",
    "query_planning",
    "architecture_routing",
    "embedding",
    "qdrant",
    "postgres_fts",
    "code_graph",
    "memory_lookup",
    "fusion",
    "reranker",
    "context_build",
    "generation",
    "citation_validation",
    "response",
)


@dataclass
class RagTimings:
    """Per-request stage timings in milliseconds (perf_counter clock)."""
    scope_resolve_ms: float = 0.0
    planning_ms: float = 0.0
    embedding_ms: float = 0.0
    qdrant_ms: float = 0.0
    postgres_fts_ms: float = 0.0
    code_graph_ms: float = 0.0
    memory_ms: float = 0.0
    fusion_ms: float = 0.0
    reranker_ms: float = 0.0
    context_build_ms: float = 0.0
    generation_ms: float = 0.0
    citation_validation_ms: float = 0.0
    total_ms: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in vars(self).items()}


class RagTrace:
    """One request's trace — timings + counters, content-free.

    ``mark(stage, ms)`` records into the matching ``RagTimings`` field;
    unknown stage names are kept under ``extra`` so nothing is dropped.
    """

    _FIELD = {
        "admission": "scope_resolve_ms",
        "authorization": "scope_resolve_ms",
        "scope": "scope_resolve_ms",
        "query_planning": "planning_ms",
        "planning": "planning_ms",
        "architecture_routing": "planning_ms",
        "embedding": "embedding_ms",
        "qdrant": "qdrant_ms",
        "postgres_fts": "postgres_fts_ms",
        "code_graph": "code_graph_ms",
        "memory_lookup": "memory_ms",
        "fusion": "fusion_ms",
        "reranker": "reranker_ms",
        "context_build": "context_build_ms",
        "generation": "generation_ms",
        "citation_validation": "citation_validation_ms",
        "response": "total_ms",
    }

    def __init__(self, request_id: str, module_ids: tuple[str, ...] = ()) -> None:
        self.request_id = request_id
        self.module_ids = module_ids
        self.timings = RagTimings()
        self.extra: dict[str, float] = {}
        self.drops: dict[str, int] = {}
        self.canonical = True
        self.hit_count = 0
        self._started = time.perf_counter()

    def mark(self, stage: str, elapsed_ms: float) -> None:
        field_name = self._FIELD.get(stage)
        if field_name is None:
            self.extra[stage] = round(elapsed_ms, 3)
            return
        setattr(self.timings, field_name,
                getattr(self.timings, field_name) + elapsed_ms)

    def drop(self, reason: str, count: int = 1) -> None:
        """Record a dropped hit: unauthorized / tombstoned /
        generation_mismatch / missing_metadata."""
        self.drops[reason] = self.drops.get(reason, 0) + count

    @contextlib.contextmanager
    def stage(self, stage: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.mark(stage, (time.perf_counter() - start) * 1000)

    def finish(self) -> dict[str, Any]:
        self.timings.total_ms = (time.perf_counter() - self._started) * 1000
        return {
            "rag_request_id": self.request_id,
            "module_ids": list(self.module_ids),
            "canonical": self.canonical,
            "hit_count": self.hit_count,
            "timings": self.timings.to_dict(),
            "extra": self.extra,
            "drops": dict(self.drops),
        }


def begin_trace(
    request_id: str, module_ids: tuple[str, ...] = ()
) -> RagTrace:
    trace = RagTrace(request_id, module_ids)
    _current_trace.set(trace)
    return trace


def current_trace() -> Optional[RagTrace]:
    return _current_trace.get()


def end_trace() -> Optional[dict[str, Any]]:
    trace = _current_trace.get()
    _current_trace.set(None)
    return trace.finish() if trace is not None else None


@contextlib.contextmanager
def timed_stage(
    stage: str, metrics: Optional["RagMetrics"] = None
) -> Iterator[None]:
    """Time one stage: mark the active trace (if any) and always record
    the sample into the metrics registry — §10.11 perf-baseline feed."""
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        trace = current_trace()
        if trace is not None:
            trace.mark(stage, elapsed_ms)
        (metrics or RAG_METRICS).observe_stage(stage, elapsed_ms)


# ---------------------------------------------------------------------------
# Metrics registry — the programmatic status surface (RAG-16B)
# ---------------------------------------------------------------------------

COUNTERS: tuple[str, ...] = (
    "rag_query_total",
    "rag_query_success_total",
    "rag_query_failed_total",
    "canonical_query_total",
    "degraded_query_total",
    "qdrant_error_total",
    "postgres_error_total",
    "embedding_error_total",
    "reranker_fallback_total",
    "retrieval_zero_result_total",
    "unauthorized_hit_dropped_total",
    "generation_mismatch_hit_dropped_total",
    "tombstoned_hit_dropped_total",
    "missing_metadata_hit_dropped_total",
)

GAUGES: tuple[str, ...] = (
    "outbox_pending",
    "outbox_retry",
    "outbox_dead_letter",
    "reconciliation_pending",
    "reconciliation_failed",
    "active_generation",
)


class RagMetrics:
    """In-process metrics registry — counters, gauges, latency samples.

    ``snapshot()`` is the programmatic read surface consumed by status()
    and the health gate; it is never the log.
    """

    def __init__(self, max_latency_samples: int = 512) -> None:
        self._lock = threading.Lock()
        self._counters = {name: 0 for name in COUNTERS}
        self._gauges: dict[str, Any] = {name: 0 for name in GAUGES}
        self._latencies: list[float] = []
        self._stage_latencies: dict[str, list[float]] = {}
        self._max_samples = max_latency_samples

    def inc(self, name: str, amount: int = 1) -> None:
        with self._lock:
            if name not in self._counters:
                raise KeyError(f"unknown rag counter: {name}")
            self._counters[name] += amount

    def set_gauge(self, name: str, value: Any) -> None:
        with self._lock:
            if name not in self._gauges:
                raise KeyError(f"unknown rag gauge: {name}")
            self._gauges[name] = value

    def observe_latency(self, ms: float) -> None:
        with self._lock:
            self._latencies.append(float(ms))
            if len(self._latencies) > self._max_samples:
                del self._latencies[: len(self._latencies) - self._max_samples]

    def observe_stage(self, stage: str, ms: float) -> None:
        """Record one per-stage latency sample (§10.11 baseline feed)."""
        with self._lock:
            samples = self._stage_latencies.setdefault(str(stage), [])
            samples.append(float(ms))
            if len(samples) > self._max_samples:
                del samples[: len(samples) - self._max_samples]

    def record_drops(self, drops: dict[str, int]) -> None:
        for reason, count in drops.items():
            counter = f"{reason}_hit_dropped_total"
            if counter in self._counters:
                self.inc(counter, count)

    def snapshot(self) -> dict[str, Any]:
        from ..perf.stats import percentile

        with self._lock:
            lat = list(self._latencies)
            stage_lat = {
                stage: list(samples)
                for stage, samples in self._stage_latencies.items()
            }
            snap: dict[str, Any] = dict(self._counters)
            snap.update(self._gauges)
        snap["retrieval_latency_p50"] = (
            percentile(lat, 0.50) if lat else 0.0
        )
        snap["retrieval_latency_p95"] = (
            percentile(lat, 0.95) if lat else 0.0
        )
        snap["stages"] = {
            stage: {
                "samples": len(samples),
                "p50_ms": percentile(samples, 0.50),
                "p95_ms": percentile(samples, 0.95),
            }
            for stage, samples in stage_lat.items()
            if samples
        }
        return snap


RAG_METRICS = RagMetrics()


__all__ = [
    "COUNTERS",
    "GAUGES",
    "RAG_METRICS",
    "RagMetrics",
    "RagTimings",
    "RagTrace",
    "TRACE_STAGES",
    "begin_trace",
    "current_trace",
    "end_trace",
    "timed_stage",
]
