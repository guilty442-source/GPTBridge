"""Distributed Tracing - Correlation ID Propagation.

Provides contextvars-based correlation ID management for request tracing
across all sovereign/gateway/runtime boundaries.
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

try:
    from typing import Self
except ImportError:
    from typing_extensions import Self


# ================================================================
# Correlation Context
# ================================================================

_correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id", default=""
)
_correlation_context_var: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "correlation_context", default={}
)


@dataclass
class CorrelationContext:
    """Full correlation context for distributed tracing."""

    correlation_id: str
    parent_id: str = ""
    trace_id: str = ""
    span_id: str = ""
    baggage: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def new(
        cls,
        correlation_id: str | None = None,
        parent_id: str = "",
        trace_id: str | None = None,
        span_id: str | None = None,
    ) -> Self:
        """Create a new correlation context."""
        cid = correlation_id or str(uuid.uuid4())
        tid = trace_id or cid.split("-")[0]  # Use first segment as trace_id
        sid = span_id or str(uuid.uuid4())[:8]
        return cls(
            correlation_id=cid,
            parent_id=parent_id,
            trace_id=tid,
            span_id=sid,
        )

    @classmethod
    def from_headers(cls, headers: dict[str, str]) -> Self:
        """Extract correlation context from HTTP/gRPC headers."""
        correlation_id = headers.get("x-correlation-id", headers.get("correlation-id", ""))
        parent_id = headers.get("x-parent-id", headers.get("parent-id", ""))
        trace_id = headers.get("x-trace-id", headers.get("trace-id", ""))
        span_id = headers.get("x-span-id", headers.get("span-id", ""))

        baggage = {}
        for k, v in headers.items():
            if k.startswith("baggage-") or k.startswith("x-baggage-"):
                baggage[k.replace("x-baggage-", "").replace("baggage-", "")] = v

        return cls(
            correlation_id=correlation_id or str(uuid.uuid4()),
            parent_id=parent_id,
            trace_id=trace_id or correlation_id or str(uuid.uuid4()),
            span_id=span_id or str(uuid.uuid4())[:8],
            baggage=baggage,
        )

    def to_headers(self) -> dict[str, str]:
        """Convert to headers for propagation."""
        headers = {
            "x-correlation-id": self.correlation_id,
            "x-trace-id": self.trace_id,
            "x-span-id": self.span_id,
        }
        if self.parent_id:
            headers["x-parent-id"] = self.parent_id
        for k, v in self.baggage.items():
            headers[f"x-baggage-{k}"] = v
        return headers

    def child(self, span_id: str | None = None) -> Self:
        """Create a child span context."""
        return CorrelationContext(
            correlation_id=self.correlation_id,
            parent_id=self.span_id,
            trace_id=self.trace_id,
            span_id=span_id or str(uuid.uuid4())[:8],
            baggage=dict(self.baggage),
            metadata=dict(self.metadata),
        )


# ================================================================
# Context Management Functions
# ================================================================


def get_correlation_id() -> str:
    """Get the current correlation ID from context."""
    return _correlation_id_var.get("")


def set_correlation_id(correlation_id: str) -> None:
    """Set the current correlation ID in context."""
    _correlation_id_var.set(correlation_id)


def clear_correlation_id() -> None:
    """Clear the current correlation ID."""
    _correlation_id_var.set("")


def get_correlation_context() -> dict[str, Any]:
    """Get the full correlation context."""
    return _correlation_context_var.get({})


def set_correlation_context(context: dict[str, Any]) -> None:
    """Set the full correlation context."""
    _correlation_context_var.set(context)


@contextmanager
def with_correlation_id(correlation_id: str | None = None) -> Callable[[], str]:
    """Context manager for correlation ID.

    Usage:
        with with_correlation_id() as get_cid:
            cid = get_cid()
            # ... do work ...
    """
    cid = correlation_id or str(uuid.uuid4())
    token = _correlation_id_var.set(cid)
    try:
        yield lambda: _correlation_id_var.get()
    finally:
        _correlation_id_var.reset(token)


def inject_correlation_headers(headers: dict[str, str]) -> dict[str, str]:
    """Inject current correlation context into headers dict."""
    cid = get_correlation_id()
    ctx = get_correlation_context()
    if cid:
        headers["x-correlation-id"] = cid
    if ctx.get("trace_id"):
        headers["x-trace-id"] = ctx["trace_id"]
    if ctx.get("span_id"):
        headers["x-span-id"] = ctx["span_id"]
    if ctx.get("parent_id"):
        headers["x-parent-id"] = ctx["parent_id"]
    for k, v in ctx.get("baggage", {}).items():
        headers[f"x-baggage-{k}"] = v
    return headers


def extract_correlation_headers(headers: dict[str, str]) -> CorrelationContext:
    """Extract correlation context from headers and set in current context."""
    context = CorrelationContext.from_headers(headers)
    set_correlation_id(context.correlation_id)
    set_correlation_context(
        {
            "correlation_id": context.correlation_id,
            "parent_id": context.parent_id,
            "trace_id": context.trace_id,
            "span_id": context.span_id,
            "baggage": context.baggage,
            "metadata": context.metadata,
        }
    )
    return context