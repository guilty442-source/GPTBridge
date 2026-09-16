"""Capacity Growth Recorder and Forecaster (A369).

Records one sample per metric per day and projects growth with
least-squares linear regression:

    metrics: db_size, index_size, wal_growth, qdrant_collection_size,
             sqlite_fleet_total, backup_size
    horizons: 30 / 90 / 180 / 365 days
    output: projected bytes, exhaustion date, confidence, assumptions

Samples live in a local SQLite store — capacity telemetry is
information-layer data, not central authority, so no migration is
required.  The forecaster never triggers cleanup; crossing a budget
only produces an archive/maintenance *proposal* upstream.

Usage:
    from shared_layer.database.growth_forecast import (
        GrowthSampleStore, forecast_metric, FORECAST_HORIZONS_DAYS,
    )
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

FORECAST_HORIZONS_DAYS: tuple[int, ...] = (30, 90, 180, 365)

GROWTH_METRICS: tuple[str, ...] = (
    "db_size",
    "index_size",
    "wal_growth",
    "qdrant_collection_size",
    "sqlite_fleet_total",
    "backup_size",
)

_SECONDS_PER_DAY = 86_400.0


class GrowthSampleStore:
    """One-sample-per-day recorder for capacity metrics."""

    def __init__(self, db_path: Path | str) -> None:
        self.connection = sqlite3.connect(str(db_path))
        self.connection.execute(
            """CREATE TABLE IF NOT EXISTS capacity_sample (
                metric TEXT NOT NULL,
                sampled_on TEXT NOT NULL,  -- YYYY-MM-DD
                value_bytes INTEGER NOT NULL,
                sampled_at TEXT NOT NULL,
                PRIMARY KEY (metric, sampled_on)
            )"""
        )
        self.connection.commit()

    def record(
        self,
        metric: str,
        value_bytes: int,
        *,
        sampled_at: Optional[float] = None,
    ) -> bool:
        """Record today's sample; returns False if today already
        has one (daily cadence — never rewrite a recorded day)."""
        if metric not in GROWTH_METRICS:
            raise ValueError(f"unknown growth metric: {metric}")
        now = sampled_at if sampled_at is not None else time.time()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO capacity_sample"
            " (metric, sampled_on, value_bytes, sampled_at)"
            " VALUES (?, ?, ?, ?)",
            (metric, day, int(value_bytes), stamp),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def series(self, metric: str) -> list[tuple[str, int]]:
        """``[(sampled_on, value_bytes)]`` ascending by day."""
        rows = self.connection.execute(
            "SELECT sampled_on, value_bytes FROM capacity_sample"
            " WHERE metric = ? ORDER BY sampled_on",
            (metric,),
        ).fetchall()
        return [(day, int(value)) for day, value in rows]

    def close(self) -> None:
        self.connection.close()


@dataclass(frozen=True)
class MetricForecast:
    """Least-squares projection for one metric."""

    metric: str
    sample_days: int
    slope_bytes_per_day: float
    projected_bytes: dict[int, int]  # horizon_days -> bytes
    exhaustion_day: Optional[str]  # first day projected >= capacity
    confidence: float  # R^2 of the fit, 0..1
    assumptions: tuple[str, ...]


def _fit(series: list[tuple[str, int]]) -> tuple[float, float, float]:
    """Return (slope bytes/day, intercept, R^2) for the series."""
    n = len(series)
    xs = [float(i) for i in range(n)]  # day index 0..n-1
    ys = [float(v) for _, v in series]
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    ss_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    ss_xx = sum((x - mean_x) ** 2 for x in xs)
    slope = ss_xy / ss_xx if ss_xx else 0.0
    intercept = mean_y - slope * mean_x
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum(
        (y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys)
    )
    r2 = 1.0 - ss_res / ss_tot if ss_tot else 1.0
    return slope, intercept, max(0.0, min(1.0, r2))


def forecast_metric(
    metric: str,
    series: list[tuple[str, int]],
    *,
    capacity_bytes: Optional[int] = None,
) -> MetricForecast:
    """Project a daily sample series across the declared horizons.

    Fewer than two samples yields a zero-growth projection with
    confidence 0 — insufficient data is stated, never guessed.
    """
    assumptions: list[str] = ["linear-trend", "daily-cadence"]
    if len(series) < 2:
        last = series[-1][1] if series else 0
        return MetricForecast(
            metric=metric,
            sample_days=len(series),
            slope_bytes_per_day=0.0,
            projected_bytes={h: last for h in FORECAST_HORIZONS_DAYS},
            exhaustion_day=None,
            confidence=0.0,
            assumptions=tuple(assumptions + ["insufficient-samples"]),
        )
    slope, intercept, r2 = _fit(series)
    n = len(series)
    projected = {
        h: max(0, int(slope * (n - 1 + h) + intercept))
        for h in FORECAST_HORIZONS_DAYS
    }
    exhaustion = None
    if capacity_bytes is not None and slope > 0:
        remaining = capacity_bytes - (slope * (n - 1) + intercept)
        if remaining <= 0:
            exhaustion = series[-1][0]
        else:
            days_left = remaining / slope
            base = time.mktime(
                time.strptime(series[-1][0], "%Y-%m-%d")
            )
            exhaustion = time.strftime(
                "%Y-%m-%d", time.gmtime(base + days_left * _SECONDS_PER_DAY)
            )
    if slope <= 0:
        assumptions.append("non-growing")
    if r2 < 0.5:
        assumptions.append("low-confidence-fit")
    return MetricForecast(
        metric=metric,
        sample_days=n,
        slope_bytes_per_day=slope,
        projected_bytes=projected,
        exhaustion_day=exhaustion,
        confidence=r2,
        assumptions=tuple(assumptions),
    )


__all__ = [
    "FORECAST_HORIZONS_DAYS",
    "GROWTH_METRICS",
    "GrowthSampleStore",
    "MetricForecast",
    "forecast_metric",
]
