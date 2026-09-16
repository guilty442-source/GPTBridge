"""SQLite Fleet Quotas (A369 capacity + A44/E30 fleet registry).

Every module's SQLite database gets a bounded quota contract:

    max_db_size             — hard cap on the main file
    max_wal_size            — hard cap on the -wal sidecar
    max_history_rows        — bounded local history
    max_pending_reconcile   — bounded reconcile backlog
    archive_threshold       — fraction of max_db_size that triggers
                              an archive proposal

Evaluation consumes ``SqliteFleetEntry`` rows from the fleet registry
plus per-member counters (history rows, pending reconcile).  A breach
produces a proposal — quota enforcement never deletes rows or files.

Usage:
    from shared_layer.database.sqlite_fleet_quota import (
        FleetQuota, MemberMetrics, evaluate_member,
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .sqlite_fleet_registry import SqliteFleetEntry


class QuotaStatus(Enum):
    WITHIN = "within"
    WARN = "warn"
    ARCHIVE_DUE = "archive-due"
    BREACH = "breach"


@dataclass(frozen=True)
class FleetQuota:
    """Per-module quota contract."""

    module_id: str
    max_db_size_bytes: int = 256 * 1024 ** 2
    max_wal_size_bytes: int = 64 * 1024 ** 2
    max_history_rows: int = 100_000
    max_pending_reconcile: int = 10_000
    archive_threshold: float = 0.70  # fraction of max_db_size
    warn_fraction: float = 0.85      # fraction of any cap

    def __post_init__(self) -> None:
        if not 0.0 < self.archive_threshold < self.warn_fraction < 1.0:
            raise ValueError(
                "archive_threshold < warn_fraction < 1 required"
            )


@dataclass(frozen=True)
class MemberMetrics:
    """Observed counters for one fleet member."""

    module_id: str
    db_size_bytes: int
    wal_size_bytes: int
    history_rows: int = 0
    pending_reconcile: int = 0


@dataclass(frozen=True)
class QuotaVerdict:
    module_id: str
    status: QuotaStatus
    breaches: tuple[str, ...]
    warnings: tuple[str, ...]
    archive_due: bool


def _from_entry(
    entry: SqliteFleetEntry,
    history_rows: int = 0,
    pending_reconcile: int = 0,
) -> MemberMetrics:
    return MemberMetrics(
        module_id=entry.owner,
        db_size_bytes=entry.size_bytes,
        wal_size_bytes=entry.wal_size_bytes,
        history_rows=history_rows,
        pending_reconcile=pending_reconcile,
    )


def evaluate_member(
    metrics: MemberMetrics, quota: Optional[FleetQuota] = None
) -> QuotaVerdict:
    """Classify one member against its quota contract."""
    quota = quota or FleetQuota(module_id=metrics.module_id)
    breaches: list[str] = []
    warnings: list[str] = []

    checks = (
        ("db_size", metrics.db_size_bytes, quota.max_db_size_bytes),
        ("wal_size", metrics.wal_size_bytes, quota.max_wal_size_bytes),
        ("history_rows", metrics.history_rows, quota.max_history_rows),
        ("pending_reconcile", metrics.pending_reconcile,
         quota.max_pending_reconcile),
    )
    for name, value, cap in checks:
        if value >= cap:
            breaches.append(f"{name}>={cap}")
        elif value >= cap * quota.warn_fraction:
            warnings.append(f"{name}>={int(cap * quota.warn_fraction)}")

    archive_due = (
        metrics.db_size_bytes
        >= quota.max_db_size_bytes * quota.archive_threshold
    )
    if breaches:
        status = QuotaStatus.BREACH
    elif archive_due:
        status = QuotaStatus.ARCHIVE_DUE
    elif warnings:
        status = QuotaStatus.WARN
    else:
        status = QuotaStatus.WITHIN
    return QuotaVerdict(
        module_id=metrics.module_id,
        status=status,
        breaches=tuple(breaches),
        warnings=tuple(warnings),
        archive_due=archive_due,
    )


def evaluate_fleet(
    entries: list[SqliteFleetEntry],
    quotas: Optional[dict[str, FleetQuota]] = None,
    counters: Optional[dict[str, tuple[int, int]]] = None,
) -> list[QuotaVerdict]:
    """Evaluate every registry entry.  ``counters`` maps owner to
    ``(history_rows, pending_reconcile)`` observed elsewhere."""
    verdicts = []
    for entry in entries:
        history, pending = (counters or {}).get(entry.owner, (0, 0))
        metrics = _from_entry(entry, history, pending)
        quota = (quotas or {}).get(entry.owner)
        verdicts.append(evaluate_member(metrics, quota))
    return verdicts


__all__ = [
    "FleetQuota",
    "MemberMetrics",
    "QuotaStatus",
    "QuotaVerdict",
    "evaluate_fleet",
    "evaluate_member",
]
