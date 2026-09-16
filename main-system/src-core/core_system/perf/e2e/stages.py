"""E2E stage adapters — each wraps a REAL primitive so measurements are
honest, not simulated:

- ts_dispatch / serialization: real json.dumps of the request payload
  (the same bytes a TypeScript client would produce) + one boundary mark.
- transport: real loopback HTTP round-trip against an in-process
  ``http.server`` bound to 127.0.0.1 (the same shape as ipcSession).
- py_validation / py_orchestration: real json.loads + command routing.
- sql_roundtrip: real sqlite3 round-trips (temp file DB — represents the
  data-IO leg; PostgreSQL-specific latency is out of scope when no DSN is
  configured and is recorded as ``engine`` metadata).
- native_*: real ``_sovereign_native.pyd`` call through the shared
  ``NativeExecutionRuntime`` — queue wait (submit→worker-start) and compute
  are measured separately, never merged.
- csharp_adapter: real subprocess spawn round-trip — the C# Windows adapter
  (GPTBridgeLauncher) is a process-boundary component; we measure the real
  OS process boundary cost as its representative.
- model_wait: pluggable callable (e.g. a governed local-model generate);
  when unavailable the phase is recorded as ``unavailable`` and excluded
  from classification rather than faked.
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
from typing import Any, Callable, Optional

from .spans import Phase, TraceContext

# Windows policy: background spawns must never open a console window.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def ts_dispatch(ctx: TraceContext, payload: dict[str, Any]) -> bytes:
    """TS-side dispatch: real serialization + boundary crossing mark."""
    with ctx.span(Phase.TS_DISPATCH) as span:
        body = json.dumps(
            {
                "command": "perf.echo",
                "payload": payload,
                "request_id": ctx.request_id,
                "correlation_id": ctx.correlation_id,
                "operation_id": ctx.operation_id,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        span.bytes = len(body)
        span.boundary_crossings = 1  # TS -> information layer
    return body


def http_transport(ctx: TraceContext, body: bytes, endpoint: str) -> dict[str, Any]:
    """Real loopback HTTP POST; server echoes the decoded payload."""
    with ctx.span(Phase.TRANSPORT) as span:
        request = urllib.request.Request(
            endpoint, data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(request, timeout=10.0) as response:
            raw = response.read()
        span.bytes = len(body) + len(raw)
        return json.loads(raw.decode("utf-8"))


def python_validation(ctx: TraceContext, body: bytes) -> dict[str, Any]:
    """Backend-side deserialize + request validation."""
    with ctx.span(Phase.PY_VALIDATION) as span:
        decoded = json.loads(body.decode("utf-8"))
        if not isinstance(decoded, dict) or "payload" not in decoded:
            raise ValueError("E2E_REQUEST_INVALID")
        span.bytes = len(body)
        span.boundary_crossings = 1  # information layer -> Python
        return decoded


def python_orchestration(
    ctx: TraceContext, decoded: dict[str, Any]
) -> dict[str, Any]:
    """Command routing / orchestration step."""
    with ctx.span(Phase.PY_ORCHESTRATION):
        payload = decoded["payload"]
        if not isinstance(payload, dict):
            raise ValueError("E2E_PAYLOAD_INVALID")
        return payload


def sql_roundtrip(
    ctx: TraceContext, conn: sqlite3.Connection, *, repeats: int = 1
) -> int:
    """Real SQL round-trips, counted individually."""
    total = 0
    with ctx.span(Phase.SQL_ROUNDTRIP) as span:
        for index in range(repeats):
            row = conn.execute(
                "SELECT value FROM perf_kv WHERE key = ?", (f"k{index}",)
            ).fetchone()
            total += len(row[0]) if row else 0
        span.sql_roundtrips = repeats
    return total


def native_call(
    ctx: TraceContext,
    runtime: Any,
    vectors: list[list[float]],
    *,
    dot_fn: Callable[[list[float], list[float]], float],
) -> list[float]:
    """Real native call through the shared bounded runtime.

    Queue wait (submit → worker start) and compute are measured separately
    by capturing the worker's own start timestamp.
    """
    worker_started: list[int] = []

    def _worker(start: int, end: int, token: Any) -> list[float]:
        worker_started.append(time.perf_counter_ns())
        with ctx.span(Phase.NATIVE_BOUNDARY) as bspan:
            left = vectors[start]
            right = vectors[min(start + 1, end - 1)]
            bspan.native_copies = 1
            bspan.native_allocs = 1
        with ctx.span(Phase.NATIVE_COMPUTE):
            return [dot_fn(left, right)]

    submitted_ns = time.perf_counter_ns()
    partials = runtime.submit_slices(
        [(0, 2)], _worker,
        deadline_at=time.monotonic() + 30.0,
        combine=lambda parts: [x for part in parts for x in part],
    )
    # Queue wait = submit → worker start only.  Recording it as its own
    # [submitted, worker_started] span keeps it from overlapping the
    # boundary/compute spans in critical-path coverage — wait is never
    # folded into execution time, and never double-counted either.
    if worker_started:
        wait_end = worker_started[0]
        ctx.record(
            Phase.NATIVE_QUEUE, submitted_ns, wait_end,
            queue_ns=wait_end - submitted_ns,
        )
    return partials


def csharp_adapter(ctx: TraceContext) -> bytes:
    """C# Windows adapter boundary — real process spawn round-trip."""
    with ctx.span(Phase.CSHARP_ADAPTER) as span:
        proc = subprocess.run(
            ["cmd.exe", "/c", "echo", "ok"],
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
            timeout=10,
        )
        span.boundary_crossings = 1  # Python -> C#/process boundary
        span.bytes = len(proc.stdout)
        return proc.stdout.strip()


def model_wait(
    ctx: TraceContext, call: Optional[Callable[[], Any]]
) -> Optional[Any]:
    """Local model wait — pluggable; marked unavailable when absent."""
    if call is None:
        with ctx.span(Phase.MODEL_WAIT) as span:
            span.meta["unavailable"] = True
            return None
    with ctx.span(Phase.MODEL_WAIT):
        return call()


def result_processing(
    ctx: TraceContext, payload: dict[str, Any]
) -> bytes:
    """Response build + serialize back toward TypeScript."""
    with ctx.span(Phase.RESULT_PROCESSING) as span:
        body = json.dumps(
            {"ok": True, "request_id": ctx.request_id, "result": payload},
            ensure_ascii=False,
        ).encode("utf-8")
        span.bytes = len(body)
        span.boundary_crossings = 1  # Python -> TS
        return body


__all__ = [
    "ts_dispatch",
    "http_transport",
    "python_validation",
    "python_orchestration",
    "sql_roundtrip",
    "native_call",
    "csharp_adapter",
    "model_wait",
    "result_processing",
]
