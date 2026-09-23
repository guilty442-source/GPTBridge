"""Batch Writer (E10).

Reconciliation, Audit, RAG metadata should not commit per-row.
Batch 100-500 rows into a single transaction, then commit.

Usage:
    from shared_layer.database.batch_writer import BatchWriter

    with BatchWriter(connection, batch_size=200) as bw:
        for row in rows:
            bw.add("INSERT INTO ... VALUES (%s, %s)", (row.a, row.b))
    # commits on exit

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A46/E22 — Audit: mandatory-ledger.
"""
from __future__ import annotations

from typing import Any

from psycopg import Connection


class BatchWriter:
    """Collect rows and flush in batches within a single transaction.

    Reduces fsync, transaction overhead, and WAL fragmentation.
    Batch size is dynamically adjustable.
    """

    def __init__(
        self,
        connection: Connection[Any],
        *,
        batch_size: int = 200,
        max_batch_size: int = 500,
        min_batch_size: int = 50,
    ) -> None:
        self._conn = connection
        self._batch_size = batch_size
        self._max_batch_size = max_batch_size
        self._min_batch_size = min_batch_size
        self._pending: list[tuple[str, tuple[Any, ...]]] = []
        self._total_flushed = 0
        self._flush_count = 0
        self._owned_tx = False

    def __enter__(self) -> "BatchWriter":
        self._owned_tx = self._conn.autocommit
        if self._owned_tx:
            self._conn.autocommit = False
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if exc_type is not None:
            self._conn.rollback()
        else:
            self.flush()
            if self._owned_tx:
                self._conn.commit()
                self._conn.autocommit = True

    def add(self, sql: str, params: tuple[Any, ...]) -> None:
        """Add a row to the pending batch.  Flushes when batch_size reached."""
        self._pending.append((sql, params))
        if len(self._pending) >= self._batch_size:
            self.flush()

    def flush(self) -> int:
        """Flush all pending rows.  Returns the number flushed."""
        if not self._pending:
            return 0
        count = len(self._pending)
        with self._conn.cursor() as cur:
            for sql, params in self._pending:
                cur.execute(sql, params)  # sql-ok: flush executes queued heterogeneous SQL in order — grouping by statement would reorder across-statement deps
        self._total_flushed += count
        self._flush_count += 1
        self._pending.clear()
        return count

    def adjust_batch_size(self, *, latency_ms: float, target_latency_ms: float = 50) -> None:
        """Dynamically adjust batch size based on observed latency.

        If latency is low, increase batch size (up to max).
        If latency is high, decrease batch size (down to min).
        """
        if latency_ms < target_latency_ms * 0.5:
            self._batch_size = min(self._batch_size + 50, self._max_batch_size)
        elif latency_ms > target_latency_ms * 2:
            self._batch_size = max(self._batch_size - 50, self._min_batch_size)

    @property
    def total_flushed(self) -> int:
        return self._total_flushed

    @property
    def flush_count(self) -> int:
        return self._flush_count

    @property
    def current_batch_size(self) -> int:
        return self._batch_size


__all__ = ["BatchWriter"]
