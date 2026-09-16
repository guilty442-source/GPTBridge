"""Degradation budgets: SQLite fallback and Qdrant indexing.

SQLite fallback: while PostgreSQL is down, the fallback may only grow inside
approved limits; past them, new writes stop (fail-closed).  Essential
traffic (audit, governance writes, critical transport) is reported and must
use its existing canonical/closed path — it never bypasses the
module-private fallback bound (A508/A512): over-limit essential writes get
``DEGRADE`` (not an allowed fallback write), never a silent extension of the
bounded buffer.

Qdrant indexing: vector upserts are rate limited and paused while transport
is peaking; PostgreSQL metadata must complete first.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import AdaptiveEnvelope, Decision, DecisionKind, LoadSignals, PriorityClass


@dataclass
class SqliteFallbackBudget:
    max_degraded_duration_seconds: float = 3600.0
    max_pending_sync: int = 10_000
    max_db_growth_bytes: int = 256 * 1024 * 1024
    max_wal_growth_bytes: int = 64 * 1024 * 1024
    initial_db_bytes: int = 0
    initial_wal_bytes: int = 0

    def exceedances(self, signals: LoadSignals) -> tuple[str, ...]:
        exceeded: list[str] = []
        if signals.degraded_seconds >= self.max_degraded_duration_seconds:
            exceeded.append("max_degraded_duration")
        if signals.sqlite_pending_count >= self.max_pending_sync:
            exceeded.append("max_pending_sync")
        db_growth = signals.sqlite_db_bytes - self.initial_db_bytes
        if db_growth >= self.max_db_growth_bytes:
            exceeded.append("max_db_growth")
        wal_growth = signals.sqlite_wal_bytes - self.initial_wal_bytes
        if wal_growth >= self.max_wal_growth_bytes:
            exceeded.append("max_wal_growth")
        return tuple(exceeded)

    def accept_write(
        self,
        signals: LoadSignals,
        priority_class: PriorityClass,
    ) -> Decision:
        exceeded = self.exceedances(signals)
        if not exceeded:
            return Decision(DecisionKind.ALLOW, "fallback:within-budget")
        if priority_class in (PriorityClass.CRITICAL, PriorityClass.INTERACTIVE):
            # A508/A512: essential traffic keeps its existing canonical path
            # and is reported, but it may not bypass the module-private
            # bounded fallback.  DEGRADE is not an allowed fallback write.
            return Decision(
                DecisionKind.DEGRADE,
                f"fallback:essential-write-over-limit:{','.join(exceeded)}",
            )
        return Decision(
            DecisionKind.REJECT,
            f"fallback:budget-exceeded:{','.join(exceeded)}",
        )


@dataclass
class QdrantIndexingBudget:
    max_batch: int = 100
    pause_transport_backlog: int = 200
    max_latency_ms: float = 5000.0

    def plan_upserts(
        self,
        signals: LoadSignals,
        *,
        metadata_pending: int,
        pending_points: int,
        envelope: AdaptiveEnvelope | None = None,
        configured_rate: float | None = None,
    ) -> tuple[int, float, str]:
        """Return ``(allowed_batch, sleep_seconds, reason)``.

        ``allowed_batch == 0`` means "do not index right now".
        """
        limits = envelope or AdaptiveEnvelope()
        if metadata_pending > 0:
            return 0, 0.5, "postgres-metadata-first"
        if signals.transport_backlog >= self.pause_transport_backlog:
            return 0, 2.0, "transport-peak-pause"
        if signals.qdrant_latency_ms >= self.max_latency_ms:
            return 0, 1.0, "qdrant-latency-pause"
        rate = limits.clamp_upsert_rate(
            configured_rate if configured_rate is not None else limits.qdrant_upserts_min_per_second
        )
        batch = max(1, min(int(self.max_batch), int(pending_points)))
        sleep_seconds = batch / rate
        return batch, round(sleep_seconds, 4), "indexing"


__all__ = ["QdrantIndexingBudget", "SqliteFallbackBudget"]
