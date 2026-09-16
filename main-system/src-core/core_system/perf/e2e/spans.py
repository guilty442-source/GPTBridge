"""E2E span model — one request_id/correlation_id/operation_id across
TypeScript → information layer → Python → SQL/native/C# → back.

Two collection modes:

- ``BENCHMARK`` (default): aggregate counters only — no per-span objects are
  retained, no logging.  Observability must never pollute the measurement.
- ``DIAGNOSTIC``: bounded ring buffer of full RequestTrace objects.  Enabled
  only when ``GPTBRIDGE_PERF_DIAGNOSTIC=1`` or ``TraceCollector`` is built
  with ``diagnostic=True``.

Queue wait and execution time are recorded as separate fields; callers that
cannot split them simply leave ``queue_ns`` at zero.
"""
from __future__ import annotations

import os
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Optional


class Phase(str, Enum):
    TS_DISPATCH = "ts_dispatch"                 # TypeScript-side dispatch
    SERIALIZATION = "serialization"             # JSON/codec both directions
    TRANSPORT = "transport"                     # loopback HTTP / IPC wire
    PY_VALIDATION = "py_validation"             # request validation
    PY_ORCHESTRATION = "py_orchestration"       # handler routing / orchestration
    SQL_ROUNDTRIP = "sql_roundtrip"             # per-round-trip measured
    NATIVE_QUEUE = "native_queue"               # submit -> worker start
    NATIVE_BOUNDARY = "native_boundary"         # buffer marshal / boundary copy
    NATIVE_COMPUTE = "native_compute"           # native execution
    CSHARP_ADAPTER = "csharp_adapter"           # C# Windows adapter boundary
    MODEL_WAIT = "model_wait"                   # local model wait
    RESULT_PROCESSING = "result_processing"     # response build / render prep


@dataclass(slots=True)
class Span:
    phase: Phase
    start_ns: int
    end_ns: int
    queue_ns: int = 0          # wait before execution, never folded into exec
    exec_ns: int = 0           # pure execution time
    bytes: int = 0             # serialization bytes attributed to this span
    boundary_crossings: int = 0
    sql_roundtrips: int = 0
    native_copies: int = 0
    native_allocs: int = 0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ns(self) -> int:
        return self.end_ns - self.start_ns


@dataclass
class RequestTrace:
    request_id: str
    correlation_id: str
    operation_id: str
    path_name: str
    started_ns: int
    ended_ns: int = 0
    spans: list[Span] = field(default_factory=list)

    @property
    def total_ns(self) -> int:
        return self.ended_ns - self.started_ns

    def phase_ns(self) -> dict[str, int]:
        """Summed durations per phase (diagnostic use only — for critical
        path analysis use the interval-coverage method instead)."""
        totals: dict[str, int] = {}
        for span in self.spans:
            totals[span.phase.value] = (
                totals.get(span.phase.value, 0) + span.duration_ns
            )
        return totals


class _Aggregate:
    """Benchmark-mode per-phase counters — O(1) memory per request."""

    __slots__ = ("count", "duration_ns", "queue_ns", "exec_ns", "bytes",
                 "boundary_crossings", "sql_roundtrips", "native_copies",
                 "native_allocs")

    def __init__(self) -> None:
        self.count = 0
        self.duration_ns = 0
        self.queue_ns = 0
        self.exec_ns = 0
        self.bytes = 0
        self.boundary_crossings = 0
        self.sql_roundtrips = 0
        self.native_copies = 0
        self.native_allocs = 0

    def add(self, span: Span) -> None:
        self.count += 1
        self.duration_ns += span.duration_ns
        self.queue_ns += span.queue_ns
        self.exec_ns += span.exec_ns
        self.bytes += span.bytes
        self.boundary_crossings += span.boundary_crossings
        self.sql_roundtrips += span.sql_roundtrips
        self.native_copies += span.native_copies
        self.native_allocs += span.native_allocs


class TraceCollector:
    """Shared sink for spans under one request identity.

    ``diagnostic`` keeps a bounded ring of full traces; benchmark mode keeps
    only phase aggregates plus per-request wall-clock totals (needed for
    percentile math — raw numbers, not span objects).
    """

    def __init__(self, *, diagnostic: Optional[bool] = None,
                 max_traces: int = 256) -> None:
        if diagnostic is None:
            diagnostic = os.environ.get("GPTBRIDGE_PERF_DIAGNOSTIC") == "1"
        self.diagnostic = diagnostic
        self._traces: deque[RequestTrace] = deque(maxlen=max_traces)
        self._aggregates: dict[str, _Aggregate] = {}
        self._e2e_ns: list[int] = []

    def begin(
        self,
        path_name: str,
        *,
        request_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        operation_id: Optional[str] = None,
    ) -> "TraceContext":
        return TraceContext(
            collector=self,
            trace=RequestTrace(
                request_id=request_id or uuid.uuid4().hex,
                correlation_id=correlation_id or uuid.uuid4().hex,
                operation_id=operation_id or uuid.uuid4().hex,
                path_name=path_name,
                started_ns=time.perf_counter_ns(),
            ),
        )

    def _emit(self, trace: RequestTrace, span: Span) -> None:
        # Spans stay in-process on the trace (needed for critical-path
        # analysis); ``diagnostic`` only controls post-close retention.
        # No logging or I/O ever happens on this hot path.
        trace.spans.append(span)
        agg = self._aggregates.setdefault(span.phase.value, _Aggregate())
        agg.add(span)

    def _close(self, trace: RequestTrace) -> None:
        self._e2e_ns.append(trace.total_ns)
        if self.diagnostic:
            self._traces.append(trace)

    # -- read surfaces ------------------------------------------------------

    def aggregates(self) -> dict[str, dict[str, int]]:
        return {
            phase: {
                "count": a.count,
                "duration_ns": a.duration_ns,
                "queue_ns": a.queue_ns,
                "exec_ns": a.exec_ns,
                "bytes": a.bytes,
                "boundary_crossings": a.boundary_crossings,
                "sql_roundtrips": a.sql_roundtrips,
                "native_copies": a.native_copies,
                "native_allocs": a.native_allocs,
            }
            for phase, a in self._aggregates.items()
        }

    def e2e_durations_ns(self) -> list[int]:
        return list(self._e2e_ns)

    def traces(self) -> list[RequestTrace]:
        """Full traces — empty unless diagnostic mode is on."""
        return list(self._traces)


class TraceContext:
    """Per-request recording surface passed down the path."""

    __slots__ = ("_collector", "trace")

    def __init__(self, collector: TraceCollector, trace: RequestTrace) -> None:
        self._collector = collector
        self.trace = trace

    @property
    def request_id(self) -> str:
        return self.trace.request_id

    @property
    def correlation_id(self) -> str:
        return self.trace.correlation_id

    @property
    def operation_id(self) -> str:
        return self.trace.operation_id

    @contextmanager
    def span(self, phase: Phase, **counters: Any) -> Iterator[Span]:
        start = time.perf_counter_ns()
        span = Span(phase=phase, start_ns=start, end_ns=start)
        try:
            yield span
        finally:
            span.end_ns = time.perf_counter_ns()
            for key, value in counters.items():
                if hasattr(span, key) and value:
                    setattr(span, key, getattr(span, key) + int(value))
            self._collector._emit(self.trace, span)

    def record(
        self, phase: Phase, start_ns: int, end_ns: int, **counters: Any
    ) -> Span:
        """Emit a pre-measured span — for waits whose end is discovered
        after the fact (e.g. queue wait = submit → worker-start)."""
        span = Span(phase=phase, start_ns=start_ns, end_ns=end_ns)
        for key, value in counters.items():
            if hasattr(span, key) and value:
                setattr(span, key, getattr(span, key) + int(value))
        self._collector._emit(self.trace, span)
        return span

    def close(self) -> None:
        self.trace.ended_ns = time.perf_counter_ns()
        self._collector._close(self.trace)


__all__ = [
    "Phase",
    "Span",
    "RequestTrace",
    "TraceCollector",
    "TraceContext",
]
