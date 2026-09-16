"""Bounded adaptive tuner for pool size, batch size and worker counts.

Rules:

  * every parameter moves only inside :class:`AdaptiveEnvelope`;
  * moves are rate-limited by ``min_dwell_seconds`` and step caps;
  * batch ladders step 50 -> 100 -> 250 (and to 500 only while fully idle);
  * latency rising shrinks batches immediately (no dwell) to protect the DB.
"""

from __future__ import annotations

import threading
import time

from .types import AdaptiveEnvelope, LoadSignals, PressureLevel


class BoundedAdaptiveTuner:
    """Deterministic, envelope-bounded parameter tuner."""

    def __init__(self, envelope: AdaptiveEnvelope | None = None) -> None:
        self.envelope = envelope or AdaptiveEnvelope()
        self._lock = threading.RLock()
        self._pool_max = self.envelope.clamp_pool(self.envelope.pool_min + 2)
        self._batch = self.envelope.batch_min
        self._workers = self.envelope.reconcile_workers_min
        self._upsert_rate = self.envelope.clamp_upsert_rate(
            self.envelope.qdrant_upserts_min_per_second * 4
        )
        self._last_move = 0.0

    # -- observations ------------------------------------------------------

    def observe(self, signals: LoadSignals, *, now: float | None = None) -> dict[str, int | float]:
        """Advance the tuner one step and return the current parameters."""
        moment = time.monotonic() if now is None else now
        pressure = signals.pressure()
        with self._lock:
            if self._latency_rising(signals):
                self._shrink_batch(aggressive=True)
                self._last_move = moment
            else:
                if moment - self._last_move >= self.envelope.min_dwell_seconds:
                    if pressure in (PressureLevel.LOW, PressureLevel.MODERATE) and not signals.degraded:
                        self._grow(moment, pressure, signals)
                    elif pressure is PressureLevel.HIGH:
                        self._shrink_light()
                    else:
                        self._shrink_batch(aggressive=True)
            self._workers = self.envelope.clamp_reconcile_workers(
                1 if pressure in (PressureLevel.HIGH, PressureLevel.CRITICAL) else self._workers
            )
            if pressure in (PressureLevel.LOW, PressureLevel.MODERATE) and not signals.degraded:
                self._workers = self.envelope.clamp_reconcile_workers(self._workers + 1)
                self._upsert_rate = self.envelope.clamp_upsert_rate(self._upsert_rate * 1.5)
            else:
                self._upsert_rate = self.envelope.clamp_upsert_rate(self._upsert_rate * 0.5)
            return self.parameters()

    def _latency_rising(self, signals: LoadSignals) -> bool:
        return (
            signals.pg_latency_ms >= self.envelope.pg_latency_high_ms
            or signals.lock_contention_pct >= self.envelope.lock_contention_high_pct
            or signals.pool_wait_timeouts > 0
            or signals.degraded
        )

    def _grow(self, moment: float, pressure: PressureLevel, signals: LoadSignals) -> None:
        self._pool_max = self.envelope.clamp_pool(self._pool_max + self.envelope.max_pool_step)
        idle = pressure is PressureLevel.LOW and signals.pg_latency_ms < self.envelope.pg_latency_moderate_ms
        if idle:
            self._batch = self.envelope.clamp_batch(min(self.envelope.batch_max, max(self._batch * 2, self._batch + self.envelope.max_batch_step)))
        elif self._batch < self.envelope.batch_min:
            self._batch = self.envelope.clamp_batch(self._batch + self.envelope.max_batch_step)
        self._last_move = moment

    def _shrink_light(self) -> None:
        self._pool_max = self.envelope.clamp_pool(self._pool_max - self.envelope.max_pool_step)
        self._batch = self.envelope.clamp_batch(int(self._batch * 0.5))

    def _shrink_batch(self, *, aggressive: bool) -> None:
        factor = 0.25 if aggressive else 0.5
        self._batch = self.envelope.clamp_batch(int(self._batch * factor))

    # -- parameters --------------------------------------------------------

    def parameters(self) -> dict[str, int | float]:
        return {
            "pool_max": self._pool_max,
            "batch_size": self._batch,
            "reconcile_workers": self._workers,
            "qdrant_upserts_per_second": round(self._upsert_rate, 3),
        }

    def pool_max(self) -> int:
        return self._pool_max

    def batch_size(self) -> int:
        return self._batch

    def reconcile_workers(self) -> int:
        return self._workers

    def upsert_rate_per_second(self) -> float:
        return self._upsert_rate

    def apply_pool_limits(self, pool) -> bool:
        """Push the tuned bounds into a pool exposing ``set_max_size``."""
        setter = getattr(pool, "set_max_size", None)
        if setter is None:
            return False
        setter(self._pool_max)
        return True


__all__ = ["BoundedAdaptiveTuner"]
