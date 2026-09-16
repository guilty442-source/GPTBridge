"""Query Fingerprint Collector (migration 033 + E3).

Collects query execution stats (latency, rows, blocks) to drive
index decisions — not guess from schema.

Usage:
    from shared_layer.database.query_fingerprint import record, get_hot

    with connection_manager.connection() as conn:
        record(conn, query_key="resource.get_by_id", latency_ms=2.5,
               rows_returned=1, rows_scanned=1)
        hot = get_hot(conn, order_by="p95")

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A46/E22 — Audit: mandatory-ledger.
"""
from __future__ import annotations

import hashlib
from typing import Any

from psycopg import Connection

from .query_allowlist import get_query

_RECORD = (
    "SELECT gptbridge_index.record_query_fingerprint("
    "%s, %s, %s, %s, %s, %s, %s)"
)
_GET_HOT = "SELECT gptbridge_index.get_hot_queries(%s, %s)"


def _hash_sql(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def record(
    connection: Connection[Any],
    *,
    query_key: str,
    latency_ms: float,
    rows_returned: int = 0,
    rows_scanned: int = 0,
    shared_blocks_hit: int = 0,
    shared_blocks_read: int = 0,
) -> None:
    """Record a query execution for fingerprint tracking."""
    sql = get_query(query_key)
    query_hash = _hash_sql(sql)
    connection.execute(
        _RECORD,
        (query_key, query_hash, latency_ms,
         rows_returned, rows_scanned,
         shared_blocks_hit, shared_blocks_read),
    )


def get_hot(
    connection: Connection[Any],
    *,
    order_by: str = "p95",
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Get hot queries by p95 latency or execution count."""
    rows = connection.execute(_GET_HOT, (order_by, limit)).fetchall()
    return [
        {
            "query_key": str(r[0]),
            "execution_count": int(r[1]),
            "mean_latency_ms": float(r[2]),
            "p95_latency_ms": float(r[3]),
            "rows_returned_total": int(r[4]),
            "rows_scanned_total": int(r[5]),
        }
        for r in rows
    ]


__all__ = ["record", "get_hot"]
