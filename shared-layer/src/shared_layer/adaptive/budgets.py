"""Degradation budgets: vector indexing.

Vector indexing: vector upserts are rate limited and paused while transport
is peaking; PostgreSQL metadata must complete first.

The retired SQLite fallback budget is gone with the storage engine: there is
no module-private fallback buffer to bound, so degraded writes are decided
directly by the data plane (fail-closed DEGRADE/REJECT).
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import AdaptiveEnvelope, LoadSignals


@dataclass
class VectorIndexingBudget:
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
        if signals.vector_latency_ms >= self.max_latency_ms:
            return 0, 1.0, "vector-latency-pause"
        rate = limits.clamp_upsert_rate(
            configured_rate if configured_rate is not None else limits.vector_upserts_min_per_second
        )
        batch = max(1, min(int(self.max_batch), int(pending_points)))
        sleep_seconds = batch / rate
        return batch, round(sleep_seconds, 4), "indexing"


__all__ = ["VectorIndexingBudget"]
