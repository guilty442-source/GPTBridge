"""Representative E2E request paths (A-order, same code each run).

Every path funnels through the same stages with the same request identity;
paths differ only in which real backends they touch:

    ui_to_python            TS dispatch → HTTP → validation → orchestration → result
    ui_to_python_sql        + real SQL round-trips
    ui_to_python_native     + shared NativeExecutionRuntime → pyd call
    ui_to_python_sql_native + SQL then native
    model_wait              + pluggable local-model call
    csharp_adapter          + real process-boundary spawn (C# adapter repr.)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import stages
from .spans import TraceContext


@dataclass
class PathContext:
    """Real services shared by all paths; harness-owned."""
    http_endpoint: Optional[str] = None
    sql_conn: Any = None
    sql_engine: str = "none"
    native_runtime: Any = None
    native_dot: Optional[Callable[[list[float], list[float]], float]] = None
    native_vectors: list[list[float]] = field(default_factory=list)
    model_call: Optional[Callable[[], Any]] = None
    sql_repeats: int = 2


PathFn = Callable[[TraceContext, PathContext], Any]


def ui_to_python(ctx: TraceContext, services: PathContext) -> Any:
    body = stages.ts_dispatch(ctx, {"echo": "ping", "n": 1})
    stages.http_transport(ctx, body, services.http_endpoint)
    decoded = stages.python_validation(ctx, body)
    payload = stages.python_orchestration(ctx, decoded)
    return stages.result_processing(ctx, payload)


def ui_to_python_sql(ctx: TraceContext, services: PathContext) -> Any:
    body = stages.ts_dispatch(ctx, {"echo": "sql", "n": 1})
    stages.http_transport(ctx, body, services.http_endpoint)
    decoded = stages.python_validation(ctx, body)
    payload = stages.python_orchestration(ctx, decoded)
    stages.sql_roundtrip(ctx, services.sql_conn, repeats=services.sql_repeats)
    return stages.result_processing(ctx, payload)


def ui_to_python_native(ctx: TraceContext, services: PathContext) -> Any:
    body = stages.ts_dispatch(ctx, {"echo": "native", "n": 1})
    stages.http_transport(ctx, body, services.http_endpoint)
    decoded = stages.python_validation(ctx, body)
    payload = stages.python_orchestration(ctx, decoded)
    stages.native_call(
        ctx, services.native_runtime, services.native_vectors,
        dot_fn=services.native_dot,
    )
    return stages.result_processing(ctx, payload)


def ui_to_python_sql_native(ctx: TraceContext, services: PathContext) -> Any:
    body = stages.ts_dispatch(ctx, {"echo": "sql+native", "n": 1})
    stages.http_transport(ctx, body, services.http_endpoint)
    decoded = stages.python_validation(ctx, body)
    payload = stages.python_orchestration(ctx, decoded)
    stages.sql_roundtrip(ctx, services.sql_conn, repeats=services.sql_repeats)
    stages.native_call(
        ctx, services.native_runtime, services.native_vectors,
        dot_fn=services.native_dot,
    )
    return stages.result_processing(ctx, payload)


def model_wait(ctx: TraceContext, services: PathContext) -> Any:
    body = stages.ts_dispatch(ctx, {"echo": "model", "n": 1})
    stages.http_transport(ctx, body, services.http_endpoint)
    decoded = stages.python_validation(ctx, body)
    payload = stages.python_orchestration(ctx, decoded)
    stages.model_wait(ctx, services.model_call)
    return stages.result_processing(ctx, payload)


def csharp_adapter(ctx: TraceContext, services: PathContext) -> Any:
    body = stages.ts_dispatch(ctx, {"echo": "csharp", "n": 1})
    stages.http_transport(ctx, body, services.http_endpoint)
    decoded = stages.python_validation(ctx, body)
    payload = stages.python_orchestration(ctx, decoded)
    stages.csharp_adapter(ctx)
    return stages.result_processing(ctx, payload)


PATHS: dict[str, PathFn] = {
    "ui_to_python": ui_to_python,
    "ui_to_python_sql": ui_to_python_sql,
    "ui_to_python_native": ui_to_python_native,
    "ui_to_python_sql_native": ui_to_python_sql_native,
    "model_wait": model_wait,
    "csharp_adapter": csharp_adapter,
}


__all__ = ["PathContext", "PathFn", "PATHS"]
