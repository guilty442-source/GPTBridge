"""Bloat Budget (A369 VACUUM-ANALYZE: threshold-triggered, bounded).

Maintenance fires when measured bloat crosses a threshold — never on
a fixed frequent schedule.  ``collect_bloat_report`` (watchdog.py)
supplies the measurements; this module classifies them into governed
maintenance proposals:

    bloat_fraction < warn     -> no action
    warn <= fraction < breach -> proposal: bounded VACUUM/ANALYZE
    fraction >= breach        -> proposal: maintenance window +
                                 REINDEX consideration (still
                                 governed, still requires approval)

The budget produces proposals only.  VACUUM/REINDEX execution stays
behind governed orchestration and user-confirmation rules (A366).

Usage:
    from shared_layer.database.bloat_budget import (
        BloatBudget, BloatMeasurement, evaluate_bloat,
    )
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MaintenanceProposal(Enum):
    NONE = "none"
    VACUUM_ANALYZE = "vacuum-analyze"
    MAINTENANCE_WINDOW = "maintenance-window"


@dataclass(frozen=True)
class BloatBudget:
    warn_fraction: float = 0.20
    breach_fraction: float = 0.40
    min_bytes: int = 64 * 1024 ** 2  # ignore tiny tables

    def __post_init__(self) -> None:
        if not 0.0 < self.warn_fraction < self.breach_fraction < 1.0:
            raise ValueError("warn < breach < 1 required")


@dataclass(frozen=True)
class BloatMeasurement:
    """One observed object — table or index."""

    object_name: str  # schema.table / schema.index
    kind: str         # "table" | "index"
    total_bytes: int
    bloat_bytes: int

    @property
    def bloat_fraction(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return self.bloat_bytes / self.total_bytes


@dataclass(frozen=True)
class BloatVerdict:
    object_name: str
    bloat_fraction: float
    proposal: MaintenanceProposal
    reason: str


def evaluate_bloat(
    measurement: BloatMeasurement,
    budget: BloatBudget | None = None,
) -> BloatVerdict:
    """Classify one object's bloat into a maintenance proposal."""
    budget = budget or BloatBudget()
    fraction = measurement.bloat_fraction
    if measurement.total_bytes < budget.min_bytes:
        return BloatVerdict(
            measurement.object_name, fraction,
            MaintenanceProposal.NONE, "below-min-size",
        )
    if fraction >= budget.breach_fraction:
        return BloatVerdict(
            measurement.object_name, fraction,
            MaintenanceProposal.MAINTENANCE_WINDOW, "bloat-breach",
        )
    if fraction >= budget.warn_fraction:
        return BloatVerdict(
            measurement.object_name, fraction,
            MaintenanceProposal.VACUUM_ANALYZE, "bloat-warn",
        )
    return BloatVerdict(
        measurement.object_name, fraction,
        MaintenanceProposal.NONE, "within-budget",
    )


def evaluate_index_ratio(
    table: str, index_bytes: int, table_bytes: int,
    *,
    warn_ratio: float = 1.0,
) -> BloatVerdict:
    """Index-size-ratio check: flag indexes outgrowing their table.

    An index larger than its base table usually means duplicate or
    unused indexes — input for the index-optimization evidence loop,
    never an automatic DROP.
    """
    if table_bytes <= 0:
        ratio = 0.0 if index_bytes == 0 else float("inf")
    else:
        ratio = index_bytes / table_bytes
    if ratio >= warn_ratio:
        return BloatVerdict(
            table, ratio, MaintenanceProposal.VACUUM_ANALYZE,
            f"index-ratio:{ratio:.2f}",
        )
    return BloatVerdict(
        table, ratio, MaintenanceProposal.NONE, "index-ratio-ok",
    )


__all__ = [
    "BloatBudget",
    "BloatMeasurement",
    "BloatVerdict",
    "MaintenanceProposal",
    "evaluate_bloat",
    "evaluate_index_ratio",
]
