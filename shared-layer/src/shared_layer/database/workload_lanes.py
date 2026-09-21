"""§10.5 SQL 連線池工作分級（`star-sql-workload/v1`）。

邏輯分級（非三個實例）：同一 ``ConnectionManager`` 上跑三條 lane——

- ``INTERACTIVE``：Chat／UI Status／User Commands——小上限、短 timeout、
  短佇列等待（互動不得被背景工作拖垮）。
- ``BACKGROUND``：RAG Indexing／Maintenance／Backup Metadata——低並發、
  長 timeout、長佇列容忍。
- ``MIGRATION``：schema 遷移——獨佔單一連線、無 statement timeout。

每條 lane 有 inflight semaphore（bounded wait，逾時 fail-visible）、
``statement_timeout``、慢查詢記錄與 pool exhaustion 統計。
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterator, Optional

_logger = logging.getLogger("gptbridge.db.workload")


class WorkloadClass(str, Enum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"
    MIGRATION = "migration"


class PoolLaneExhausted(RuntimeError):
    """Lane 佇列等待逾時——fail-visible，呼叫端可降級或回報。"""


@dataclass(frozen=True)
class LanePolicy:
    max_inflight: int
    statement_timeout_ms: int  # 0 = 不設限（migration）
    slow_query_ms: int
    queue_timeout_s: float


DEFAULT_LANES: dict[WorkloadClass, LanePolicy] = {
    WorkloadClass.INTERACTIVE: LanePolicy(
        max_inflight=6,
        statement_timeout_ms=5_000,
        slow_query_ms=200,
        queue_timeout_s=5.0,
    ),
    WorkloadClass.BACKGROUND: LanePolicy(
        max_inflight=2,
        statement_timeout_ms=60_000,
        slow_query_ms=2_000,
        queue_timeout_s=30.0,
    ),
    WorkloadClass.MIGRATION: LanePolicy(
        max_inflight=1,
        statement_timeout_ms=0,
        slow_query_ms=5_000,
        queue_timeout_s=60.0,
    ),
}


@dataclass
class LaneStats:
    acquisitions: int = 0
    inflight: int = 0
    queue_timeouts: int = 0
    slow_queries: int = 0
    statement_timeouts: int = 0
    queries: int = 0
    total_query_ms: float = 0.0


class WorkloadLanePool:
    """在同一個 ConnectionManager 上做邏輯分級。"""

    def __init__(
        self,
        manager: Any,
        lanes: Optional[dict[WorkloadClass, LanePolicy]] = None,
    ) -> None:
        self._manager = manager
        self._lanes = dict(lanes or DEFAULT_LANES)
        self._semaphores = {
            cls: threading.BoundedSemaphore(policy.max_inflight)
            for cls, policy in self._lanes.items()
        }
        self._stats = {cls: LaneStats() for cls in self._lanes}
        self._stats_lock = threading.Lock()

    # -- lane acquisition ---------------------------------------------------

    @contextmanager
    def connection(
        self, workload: WorkloadClass | str
    ) -> Iterator[Any]:
        """取得一條 lane 連線（bounded wait + statement_timeout）。

        佇列逾時丟 ``PoolLaneExhausted``——不得無限等待拖垮互動 lane。
        """
        cls = (
            workload
            if isinstance(workload, WorkloadClass)
            else WorkloadClass(str(workload))
        )
        policy = self._lanes[cls]
        semaphore = self._semaphores[cls]
        stats = self._stats[cls]
        if not semaphore.acquire(timeout=policy.queue_timeout_s):
            with self._stats_lock:
                stats.queue_timeouts += 1
            raise PoolLaneExhausted(
                f"lane {cls.value} queue timeout after {policy.queue_timeout_s}s"
            )
        with self._stats_lock:
            stats.acquisitions += 1
            stats.inflight += 1
        try:
            with self._manager.connection() as conn:
                if policy.statement_timeout_ms:
                    conn.execute(
                        f"SET statement_timeout = {int(policy.statement_timeout_ms)}"
                    )
                yield _LaneConnection(conn, policy, stats, self._stats_lock)
        finally:
            with self._stats_lock:
                stats.inflight -= 1
            semaphore.release()

    def execute(
        self,
        workload: WorkloadClass | str,
        sql: str,
        params: tuple | list | None = None,
    ) -> list[dict[str, Any]]:
        """一次性查詢：取 lane 連線 → 計時 → 慢查詢記錄 → 回傳列。"""
        with self.connection(workload) as conn:
            return conn.execute(sql, params).fetchall()

    # -- observability ------------------------------------------------------

    def stats(self) -> dict[str, dict[str, Any]]:
        with self._stats_lock:
            return {
                cls.value: {
                    "acquisitions": s.acquisitions,
                    "inflight": s.inflight,
                    "queue_timeouts": s.queue_timeouts,
                    "slow_queries": s.slow_queries,
                    "statement_timeouts": s.statement_timeouts,
                    "queries": s.queries,
                    "avg_query_ms": round(
                        s.total_query_ms / s.queries, 3
                    )
                    if s.queries
                    else 0.0,
                }
                for cls, s in self._stats.items()
            }


class _LaneConnection:
    """包裝 psycopg Connection：計時＋慢查詢記錄＋timeout 統計。"""

    def __init__(
        self,
        conn: Any,
        policy: LanePolicy,
        stats: LaneStats,
        stats_lock: threading.Lock,
    ) -> None:
        self._conn = conn
        self._policy = policy
        self._stats = stats
        self._lock = stats_lock

    def execute(self, sql: str, params: Any = None) -> Any:
        started = time.monotonic()
        try:
            return self._conn.execute(sql, params)
        except Exception as exc:
            if "statement timeout" in str(exc).lower() or "canceling statement" in str(exc):
                with self._lock:
                    self._stats.statement_timeouts += 1
            raise
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000.0
            with self._lock:
                self._stats.queries += 1
                self._stats.total_query_ms += elapsed_ms
            if elapsed_ms >= self._policy.slow_query_ms:
                with self._lock:
                    self._stats.slow_queries += 1
                _logger.warning(
                    "slow query (%.1fms >= %dms): %.160s",
                    elapsed_ms,
                    self._policy.slow_query_ms,
                    sql,
                )

    def cursor(self) -> Any:
        return self._conn.cursor()

    @property
    def raw(self) -> Any:
        return self._conn


__all__ = [
    "DEFAULT_LANES",
    "LanePolicy",
    "PoolLaneExhausted",
    "WorkloadClass",
    "WorkloadLanePool",
]
