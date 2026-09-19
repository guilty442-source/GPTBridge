"""OpenTelemetry Integration for GPTBridge.

Provides automatic instrumentation for traces, spans, and metrics export.
Integrates with existing correlation context and metrics infrastructure.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import wraps
from typing import Any, Callable, Optional

# Lazy import to avoid hard dependency
_OTEL_AVAILABLE = False
_tracer = None
_meter = None
_propagator = None


def _ensure_otel() -> bool:
    """Initialize OpenTelemetry SDK if available."""
    global _OTEL_AVAILABLE, _tracer, _meter, _propagator
    if _OTEL_AVAILABLE:
        return True
    try:
        from opentelemetry import trace, metrics
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader, ConsoleMetricExporter
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.propagators.textmap import TextMapPropagator

        # Resource with service name
        resource = Resource.create({SERVICE_NAME: "gptbridge"})

        # Trace provider
        trace_provider = TracerProvider(resource=resource)
        trace_provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        trace.set_tracer_provider(trace_provider)

        # Metrics provider
        metric_reader = PeriodicExportingMetricReader(ConsoleMetricExporter())
        metric_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(metric_provider)

        # Propagator for context injection/extraction
        class CorrelationPropagator(TextMapPropagator):
            def inject(self, carrier: dict, context: Optional[dict] = None) -> None:
                from shared_layer.observability.tracing import inject_correlation_headers
                if context:
                    inject_correlation_headers(carrier)

            def extract(self, carrier: dict, context: Optional[dict] = None) -> Optional[dict]:
                from shared_layer.observability.tracing import extract_correlation_headers
                return extract_correlation_headers(carrier) if carrier else None

            def fields(self) -> set:
                return {"x-correlation-id", "x-trace-id", "x-span-id", "x-parent-id"}

        set_global_textmap(CorrelationPropagator())

        _tracer = trace.get_tracer("gptbridge")
        _meter = metrics.get_meter("gptbridge")
        _OTEL_AVAILABLE = True
        return True
    except ImportError:
        return False


def get_tracer():
    """Get the OpenTelemetry tracer."""
    if not _OTEL_AVAILABLE:
        _ensure_otel()
    return _tracer


def get_meter():
    """Get the OpenTelemetry meter."""
    if not _OTEL_AVAILABLE:
        _ensure_otel()
    return _meter


def is_otel_available() -> bool:
    """Check if OpenTelemetry is available."""
    return _OTEL_AVAILABLE or _ensure_otel()


# ================================================================
# Span Helpers
# ================================================================

@contextmanager
def trace_span(name: str, attributes: Optional[dict] = None, kind=None):
    """Context manager for creating spans."""
    tracer = get_tracer()
    if tracer and _OTEL_AVAILABLE:
        from opentelemetry.trace import SpanKind
        span_kind = kind or SpanKind.INTERNAL
        with tracer.start_as_current_span(name, kind=span_kind, attributes=attributes or {}) as span:
            yield span
    else:
        # No-op context manager
        class NoOpSpan:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def set_attribute(self, k, v): pass
            def add_event(self, name, attrs=None): pass
            def record_exception(self, exc): pass
            def set_status(self, status): pass
        yield NoOpSpan()


def traced(name: Optional[str] = None, attributes: Optional[dict] = None, kind=None):
    """Decorator for automatic span creation."""
    def decorator(func: Callable) -> Callable:
        span_name = name or f"{func.__module__}.{func.__qualname__}"
        @wraps(func)
        def wrapper(*args, **kwargs):
            with trace_span(span_name, attributes=attributes) as span:
                try:
                    result = func(*args, **kwargs)
                    if span and _OTEL_AVAILABLE:
                        span.set_attribute("success", True)
                    return result
                except Exception as e:
                    if span and _OTEL_AVAILABLE:
                        span.set_attribute("success", False)
                        span.set_attribute("error", str(e))
                        span.record_exception(e)
                    raise
        return wrapper
    return decorator


# ================================================================
# Metric Helpers
# ================================================================

def create_counter(name: str, description: str = "", unit: str = "1"):
    """Create a counter metric."""
    meter = get_meter()
    if meter and _OTEL_AVAILABLE:
        return meter.create_counter(name, description=description, unit=unit)
    return None


def create_histogram(name: str, description: str = "", unit: str = "ms"):
    """Create a histogram metric."""
    meter = get_meter()
    if meter and _OTEL_AVAILABLE:
        return meter.create_histogram(name, description=description, unit=unit)
    return None


def create_gauge(name: str, description: str = "", unit: str = "1"):
    """Create a gauge metric."""
    meter = get_meter()
    if meter and _OTEL_AVAILABLE:
        return meter.create_observable_gauge(name, description=description, unit=unit)
    return None


# ================================================================
# Integration Helpers
# ================================================================

def inject_trace_context(carrier: dict) -> dict:
    """Inject trace context into carrier (headers, etc.)."""
    if not _OTEL_AVAILABLE:
        return carrier
    from opentelemetry import context as otel_context
    from opentelemetry.propagate import inject
    inject(carrier, context=otel_context.get_current())
    return carrier


def extract_trace_context(carrier: dict) -> Optional[Any]:
    """Extract trace context from carrier."""
    if not _OTEL_AVAILABLE:
        return None
    from opentelemetry import context as otel_context
    from opentelemetry.propagate import extract
    return extract(carrier, context=otel_context.get_current())


# ================================================================
# Auto-instrumentation for common patterns
# ================================================================

def instrument_async_function(name: Optional[str] = None, attributes: Optional[dict] = None):
    """Instrument an async function with tracing."""
    def decorator(func: Callable) -> Callable:
        span_name = name or f"{func.__module__}.{func.__qualname__}"
        @wraps(func)
        async def wrapper(*args, **kwargs):
            with trace_span(span_name, attributes=attributes) as span:
                try:
                    result = await func(*args, **kwargs)
                    if span and _OTEL_AVAILABLE:
                        span.set_attribute("success", True)
                    return result
                except Exception as e:
                    if span and _OTEL_AVAILABLE:
                        span.set_attribute("success", False)
                        span.set_attribute("error", str(e))
                        span.record_exception(e)
                    raise
        return wrapper
    return decorator


def instrument_ipc_handler(handler_name: str):
    """Instrument an IPC handler with standard attributes."""
    return instrument_async_function(
        f"ipc.{handler_name}",
        attributes={"component": "ipc", "handler": handler_name}
    )


def instrument_tool_call(tool_id: str, command: str):
    """Instrument a tool call with tracing."""
    return trace_span(
        f"tool.{tool_id}.{command}",
        attributes={
            "component": "toolbox",
            "tool_id": tool_id,
            "command": command,
        }
    )


def instrument_db_operation(operation: str, table: Optional[str] = None):
    """Instrument a database operation with tracing."""
    return trace_span(
        f"db.{operation}",
        attributes={
            "component": "database",
            "operation": operation,
            **({"table": table} if table else {}),
        }
    )


__all__ = [
    "trace_span",
    "traced",
    "instrument_async_function",
    "instrument_ipc_handler",
    "instrument_tool_call",
    "instrument_db_operation",
    "create_counter",
    "create_histogram",
    "create_gauge",
    "inject_trace_context",
    "extract_trace_context",
    "get_tracer",
    "get_meter",
    "is_otel_available",
]