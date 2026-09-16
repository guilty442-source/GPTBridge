"""WAL Budget Evaluator (A369 capacity + E9 WAL governance).

PostgreSQL budget dimensions:
    wal_bytes_per_hour      — generated WAL volume per hour
    checkpoint_interval_s   — observed seconds between checkpoints
    archive_backlog_bytes   — WAL written but not yet archived
    slot_retained_bytes     — bytes pinned by replication slots
                             (reserved for future replication)

SQLite budget dimensions (per registered fleet member):
    wal_size_bytes          — current -wal file size
    checkpoint_age_seconds  — time since last successful checkpoint
    busy_lock_rate          — busy/lock events per observed operation

Each dimension evaluates to ``ok`` / ``warn`` / ``breach``.  A breach
only produces a maintenance proposal upstream — the evaluator never
runs a checkpoint or clears an archive itself.

Usage:
    from shared_layer.database.wal_budget import (
        PgWalBudget, PgWalMetrics, evaluate_pg_wal,
        SqliteWalBudget, SqliteWalMetrics, evaluate_sqlite_wal,
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class BudgetStatus(Enum):
    OK = "ok"
    WARN = "warn"
    BREACH = "breach"


@dataclass(frozen=True)
class PgWalBudget:
    max_wal_bytes_per_hour: int = 4 * 1024 ** 3
    warn_wal_bytes_per_hour: int = 2 * 1024 ** 3
    max_checkpoint_interval_s: float = 1800.0
    warn_checkpoint_interval_s: float = 900.0
    max_archive_backlog_bytes: int = 8 * 1024 ** 3
    warn_archive_backlog_bytes: int = 4 * 1024 ** 3
    max_slot_retained_bytes: int = 16 * 1024 ** 3
    warn_slot_retained_bytes: int = 8 * 1024 ** 3


@dataclass(frozen=True)
class PgWalMetrics:
    wal_bytes_per_hour: float
    checkpoint_interval_s: float
    archive_backlog_bytes: int
    slot_retained_bytes: int = 0


@dataclass(frozen=True)
class DimensionVerdict:
    dimension: str
    value: float
    status: BudgetStatus
    warn_threshold: float
    breach_threshold: float


@dataclass(frozen=True)
class WalReport:
    subject: str
    verdicts: tuple[DimensionVerdict, ...]

    @property
    def status(self) -> BudgetStatus:
        order = [BudgetStatus.OK, BudgetStatus.WARN, BudgetStatus.BREACH]
        return max(
            (v.status for v in self.verdicts),
            key=order.index,
            default=BudgetStatus.OK,
        )


def _verdict(
    dimension: str, value: float, warn: float, breach: float
) -> DimensionVerdict:
    if value >= breach:
        status = BudgetStatus.BREACH
    elif value >= warn:
        status = BudgetStatus.WARN
    else:
        status = BudgetStatus.OK
    return DimensionVerdict(dimension, value, status, warn, breach)


def evaluate_pg_wal(
    metrics: PgWalMetrics, budget: PgWalBudget | None = None
) -> WalReport:
    """Evaluate observed PostgreSQL WAL behaviour against budget."""
    budget = budget or PgWalBudget()
    return WalReport(
        subject="postgresql",
        verdicts=(
            _verdict("wal_bytes_per_hour", metrics.wal_bytes_per_hour,
                     budget.warn_wal_bytes_per_hour,
                     budget.max_wal_bytes_per_hour),
            _verdict("checkpoint_interval_s", metrics.checkpoint_interval_s,
                     budget.warn_checkpoint_interval_s,
                     budget.max_checkpoint_interval_s),
            _verdict("archive_backlog_bytes", metrics.archive_backlog_bytes,
                     budget.warn_archive_backlog_bytes,
                     budget.max_archive_backlog_bytes),
            _verdict("slot_retained_bytes", metrics.slot_retained_bytes,
                     budget.warn_slot_retained_bytes,
                     budget.max_slot_retained_bytes),
        ),
    )


@dataclass(frozen=True)
class SqliteWalBudget:
    max_wal_size_bytes: int = 64 * 1024 ** 2
    warn_wal_size_bytes: int = 32 * 1024 ** 2
    max_checkpoint_age_s: float = 3600.0
    warn_checkpoint_age_s: float = 1800.0
    max_busy_lock_rate: float = 0.05
    warn_busy_lock_rate: float = 0.01


@dataclass(frozen=True)
class SqliteWalMetrics:
    database_path: str
    wal_size_bytes: int
    checkpoint_age_seconds: float
    busy_lock_rate: float


def evaluate_sqlite_wal(
    metrics: SqliteWalMetrics, budget: SqliteWalBudget | None = None
) -> WalReport:
    """Evaluate one SQLite fleet member's WAL behaviour."""
    budget = budget or SqliteWalBudget()
    return WalReport(
        subject=metrics.database_path,
        verdicts=(
            _verdict("wal_size_bytes", metrics.wal_size_bytes,
                     budget.warn_wal_size_bytes,
                     budget.max_wal_size_bytes),
            _verdict("checkpoint_age_s", metrics.checkpoint_age_seconds,
                     budget.warn_checkpoint_age_s,
                     budget.max_checkpoint_age_s),
            _verdict("busy_lock_rate", metrics.busy_lock_rate,
                     budget.warn_busy_lock_rate,
                     budget.max_busy_lock_rate),
        ),
    )


__all__ = [
    "BudgetStatus",
    "DimensionVerdict",
    "PgWalBudget",
    "PgWalMetrics",
    "SqliteWalBudget",
    "SqliteWalMetrics",
    "WalReport",
    "evaluate_pg_wal",
    "evaluate_sqlite_wal",
]
