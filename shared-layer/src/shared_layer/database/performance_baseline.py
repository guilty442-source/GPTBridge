"""Performance Baseline Collector (migration 039 + E14).

Records baseline metrics so future changes can answer
"did this make things faster or slower?"

Usage:
    from shared_layer.database.performance_baseline import record, compare

    with connection_manager.connection() as conn:
        record(conn, operation="resource_lookup", p50=2.1, p95=5.3, p99=8.7,
               sample_count=1000)
        delta = compare(conn, operation="resource_lookup")

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A46/E22 — Audit: mandatory-ledger.
"""
from __future__ import annotations

from typing import Any, Optional

from psycopg import Connection

_RECORD = (
    "SELECT gptbridge_index.record_baseline(%s, %s, %s, %s, %s, %s)"
)
_GET_LATEST = "SELECT gptbridge_index.get_latest_baseline(%s)"
_COMPARE = "SELECT gptbridge_index.compare_baseline(%s, %s, %s)"


def record(
    connection: Connection[Any],
    *,
    operation: str,
    p50_ms: float,
    p95_ms: float,
    p99_ms: float,
    sample_count: int,
    description: Optional[str] = None,
) -> None:
    """Record a performance baseline measurement."""
    connection.execute(
        _RECORD,
        (operation, p50_ms, p95_ms, p99_ms, sample_count, description),
    )


def get_latest(
    connection: Connection[Any],
    *,
    operation: str,
) -> Optional[dict[str, Any]]:
    """Get the most recent baseline for an operation."""
    row = connection.execute(_GET_LATEST, (operation,)).fetchone()
    if not row or row[0] is None:
        return None
    return {
        "p50_latency_ms": float(row[0]),
        "p95_latency_ms": float(row[1]),
        "p99_latency_ms": float(row[2]),
        "sample_count": int(row[3]),
        "baseline_at": row[4],
    }


def compare(
    connection: Connection[Any],
    *,
    operation: str,
    before_at: Optional[Any] = None,
    after_at: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """Compare two baselines for an operation."""
    row = connection.execute(
        _COMPARE, (operation, before_at, after_at)
    ).fetchone()
    if not row or row[0] is None:
        return None
    return {
        "before_p50_ms": float(row[0]),
        "after_p50_ms": float(row[1]),
        "p50_delta_pct": float(row[2]),
        "before_p95_ms": float(row[3]),
        "after_p95_ms": float(row[4]),
        "p95_delta_pct": float(row[5]),
    }


__all__ = ["record", "get_latest", "compare"]
