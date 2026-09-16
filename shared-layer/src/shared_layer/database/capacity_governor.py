"""Capacity Governor — unified data-layer capacity facade (A369).

One entry point answers the questions this phase exists for:

    how much is used        -> watermarks per store target
    how fast it grows       -> growth forecast 30/90/180/365d
    when it will fill       -> exhaustion dates per metric
    what costs the most     -> per-target usage + index ratios
    headroom under failure  -> emergency disk level + release
                               capacity certification

The governor evaluates and *proposes*.  Archive, purge, maintenance
and denial-of-write decisions it emits are proposals for governed
execution — never silent destructive cleanup (A369 FORBID).

Usage:
    from shared_layer.database.capacity_governor import (
        CapacitySnapshot, CapacityReport, evaluate_capacity,
    )
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .capacity_watermark import (
    WatermarkReport, evaluate_all,
)
from .emergency_disk import (
    DiskVerdict, EmergencyDiskPolicy, EmergencyLevel, evaluate_disk,
)
from .growth_forecast import (
    MetricForecast, forecast_metric,
)
from .retention_governor import (
    RetentionProposal,
)


@dataclass(frozen=True)
class CapacitySnapshot:
    """One observation of the whole data layer.

    ``usage`` maps watermark target -> (used_bytes, capacity_bytes).
    ``series`` maps growth metric -> [(day, bytes)] daily samples.
    ``capacities`` maps growth metric -> provisioned capacity bytes
    (drives exhaustion-date projection).
    """

    usage: dict[str, tuple[int, int]]
    free_bytes: int
    total_bytes: int
    series: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    capacities: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class CapacityReport:
    watermarks: WatermarkReport
    disk: DiskVerdict
    forecasts: tuple[MetricForecast, ...]
    proposals: tuple[RetentionProposal, ...]

    @property
    def summary(self) -> dict:
        return {
            "worst_watermark": self.watermarks.worst.value,
            "fail_closed_targets": list(
                self.watermarks.fail_closed_targets
            ),
            "emergency_level": self.disk.level.value,
            "free_fraction": round(self.disk.free_fraction, 4),
            "metrics": {
                f.metric: {
                    "slope_bytes_per_day": round(
                        f.slope_bytes_per_day, 1
                    ),
                    "projected": f.projected_bytes,
                    "exhaustion_day": f.exhaustion_day,
                    "confidence": round(f.confidence, 3),
                }
                for f in self.forecasts
            },
            "proposals": [
                {"domain": p.domain, "action": p.action.value,
                 "reason": p.reason}
                for p in self.proposals
            ],
        }


def evaluate_capacity(
    snapshot: CapacitySnapshot,
    *,
    disk_policy: Optional[EmergencyDiskPolicy] = None,
    extra_proposals: tuple[RetentionProposal, ...] = (),
) -> CapacityReport:
    """Evaluate one snapshot into a full capacity report.

    Emergency state suppresses forecast-driven proposals — while the
    disk is critical, archiving proposals defer to the emergency shed
    order rather than racing for the same bytes.
    """
    watermarks = evaluate_all(snapshot.usage)
    disk = evaluate_disk(
        snapshot.free_bytes, snapshot.total_bytes, disk_policy
    )
    forecasts = tuple(
        forecast_metric(
            metric, series,
            capacity_bytes=snapshot.capacities.get(metric),
        )
        for metric, series in sorted(snapshot.series.items())
    )
    proposals = (
        () if disk.level in (EmergencyLevel.CRITICAL,
                             EmergencyLevel.FAIL_CLOSED)
        else extra_proposals
    )
    return CapacityReport(watermarks, disk, forecasts, proposals)


__all__ = [
    "CapacityReport",
    "CapacitySnapshot",
    "evaluate_capacity",
]
