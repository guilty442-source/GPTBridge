"""SSD Write Governor (A369 QUERY-BUDGET / backpressure).

High-write sources — PostgreSQL WAL, audit, transport, reconcile,
SQLite WAL, Qdrant reindex — must be time-sliced, rate-limited and
batched so they never burst onto the SSD simultaneously.

Model:

* each source has a token-bucket rate budget (bytes/second),
* each source has a maximum single-batch byte cap,
* sources in the same ``exclusion_group`` may not hold a write window
  at the same time — a second request is deferred, not denied,
* every admit/defer decision is returned with ``retry_after_seconds``
  so callers can schedule instead of spin.

The governor admits or defers work; it never executes writes itself
and never kills in-flight I/O.  Emergency denial stays reversible,
visible and auditable (A369).

Usage:
    from shared_layer.database.write_governor import (
        WriteGovernor, WriteSourcePolicy, WriteDecision,
    )
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

WRITE_SOURCES: tuple[str, ...] = (
    "pg_wal",
    "audit",
    "transport",
    "reconcile",
    "sqlite_wal",
    "qdrant_reindex",
)


class WriteDecisionKind(Enum):
    ALLOW = "allow"
    DEFER = "defer"    # window busy or bucket empty — retry later
    DENY = "deny"      # source disabled (e.g. emergency disk policy)


@dataclass(frozen=True)
class WriteSourcePolicy:
    """Rate/batch/window contract for one write source."""

    source: str
    rate_bytes_per_second: float
    max_batch_bytes: int
    exclusion_group: Optional[str] = None
    enabled: bool = True


@dataclass(frozen=True)
class WriteDecision:
    kind: WriteDecisionKind
    source: str
    granted_bytes: int
    retry_after_seconds: float
    reason: str


#: Sensible defaults; deployments override via registry codes (A386).
DEFAULT_POLICIES: dict[str, WriteSourcePolicy] = {
    "pg_wal": WriteSourcePolicy(
        "pg_wal", rate_bytes_per_second=64 * 1024 * 1024,
        max_batch_bytes=64 * 1024 * 1024, exclusion_group="bulk",
    ),
    "audit": WriteSourcePolicy(
        "audit", rate_bytes_per_second=16 * 1024 * 1024,
        max_batch_bytes=8 * 1024 * 1024,
    ),
    "transport": WriteSourcePolicy(
        "transport", rate_bytes_per_second=32 * 1024 * 1024,
        max_batch_bytes=16 * 1024 * 1024,
    ),
    "reconcile": WriteSourcePolicy(
        "reconcile", rate_bytes_per_second=16 * 1024 * 1024,
        max_batch_bytes=8 * 1024 * 1024, exclusion_group="bulk",
    ),
    "sqlite_wal": WriteSourcePolicy(
        "sqlite_wal", rate_bytes_per_second=32 * 1024 * 1024,
        max_batch_bytes=16 * 1024 * 1024,
    ),
    "qdrant_reindex": WriteSourcePolicy(
        "qdrant_reindex", rate_bytes_per_second=64 * 1024 * 1024,
        max_batch_bytes=64 * 1024 * 1024, exclusion_group="bulk",
    ),
}


class _Bucket:
    """Token bucket state for one source."""

    __slots__ = ("tokens", "updated_at")

    def __init__(self, capacity: float) -> None:
        self.tokens = capacity
        self.updated_at = time.monotonic()


class WriteGovernor:
    """Admits write work under per-source rate/batch/window budgets."""

    def __init__(
        self,
        policies: Optional[dict[str, WriteSourcePolicy]] = None,
        *,
        clock=time.monotonic,
    ) -> None:
        self._policies = dict(policies or DEFAULT_POLICIES)
        self._clock = clock
        self._buckets = {
            name: _Bucket(policy.rate_bytes_per_second)
            for name, policy in self._policies.items()
        }
        self._window_holders: dict[str, str] = {}  # group -> source

    def admit(self, source: str, requested_bytes: int) -> WriteDecision:
        """Decide whether ``source`` may write ``requested_bytes`` now."""
        policy = self._policies.get(source)
        if policy is None:
            return WriteDecision(
                WriteDecisionKind.DENY, source, 0, 0.0, "unknown-source"
            )
        if not policy.enabled:
            return WriteDecision(
                WriteDecisionKind.DENY, source, 0, 0.0, "source-disabled"
            )
        granted = min(requested_bytes, policy.max_batch_bytes)
        window = self._window_check(policy)
        if window is not None:
            return window
        return self._bucket_check(policy, granted)

    def release_window(self, source: str) -> None:
        """Release the caller's exclusion-group window after I/O."""
        policy = self._policies.get(source)
        if policy and policy.exclusion_group:
            group = policy.exclusion_group
            if self._window_holders.get(group) == source:
                del self._window_holders[group]

    def set_enabled(self, source: str, enabled: bool) -> None:
        """Toggle a source (emergency disk policy uses this)."""
        policy = self._policies.get(source)
        if policy is None:
            raise KeyError(source)
        self._policies[source] = WriteSourcePolicy(
            policy.source, policy.rate_bytes_per_second,
            policy.max_batch_bytes, policy.exclusion_group, enabled,
        )

    def _window_check(self, policy: WriteSourcePolicy) -> Optional[WriteDecision]:
        if not policy.exclusion_group:
            return None
        holder = self._window_holders.get(policy.exclusion_group)
        if holder is None or holder == policy.source:
            self._window_holders[policy.exclusion_group] = policy.source
            return None
        return WriteDecision(
            WriteDecisionKind.DEFER, policy.source, 0, 1.0,
            f"window-held-by:{holder}",
        )

    def _bucket_check(
        self, policy: WriteSourcePolicy, granted: int
    ) -> WriteDecision:
        bucket = self._buckets[policy.source]
        now = self._clock()
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.updated_at = now
        bucket.tokens = min(
            policy.rate_bytes_per_second,
            bucket.tokens + elapsed * policy.rate_bytes_per_second,
        )
        if granted <= bucket.tokens:
            bucket.tokens -= granted
            return WriteDecision(
                WriteDecisionKind.ALLOW, policy.source, granted, 0.0, "ok"
            )
        deficit = granted - bucket.tokens
        retry = deficit / policy.rate_bytes_per_second
        return WriteDecision(
            WriteDecisionKind.DEFER, policy.source, 0, retry,
            "rate-budget",
        )


__all__ = [
    "DEFAULT_POLICIES",
    "WRITE_SOURCES",
    "WriteDecision",
    "WriteDecisionKind",
    "WriteGovernor",
    "WriteSourcePolicy",
]
