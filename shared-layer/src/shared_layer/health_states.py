"""Unified health vocabulary for every data component.

Modules may not invent their own health strings: a component reports one of
the six :class:`HealthState` values plus a :class:`ReasonCode`, and the
aggregate is computed from those.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class HealthState(Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    DRIFTED = "DRIFTED"
    RECOVERING = "RECOVERING"
    QUARANTINED = "QUARANTINED"
    UNKNOWN = "UNKNOWN"


class ReasonCode(Enum):
    PG_POOL_EXHAUSTED = "PG_POOL_EXHAUSTED"
    PG_LOCK_CONTENTION = "PG_LOCK_CONTENTION"
    PG_UNAVAILABLE = "PG_UNAVAILABLE"
    SQLITE_BUSY = "SQLITE_BUSY"
    SQLITE_WAL_PRESSURE = "SQLITE_WAL_PRESSURE"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"
    RLS_DRIFT = "RLS_DRIFT"
    RECONCILE_BACKLOG = "RECONCILE_BACKLOG"
    QDRANT_INDEX_LAG = "QDRANT_INDEX_LAG"
    QDRANT_UNAVAILABLE = "QDRANT_UNAVAILABLE"
    BACKUP_STALE = "BACKUP_STALE"
    TRANSPORT_BACKLOG = "TRANSPORT_BACKLOG"
    GENERATION_STALE = "GENERATION_STALE"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


_SEVERITY: Final[dict[HealthState, int]] = {
    HealthState.HEALTHY: 0,
    HealthState.UNKNOWN: 1,
    HealthState.RECOVERING: 2,
    HealthState.DEGRADED: 3,
    HealthState.DRIFTED: 4,
    HealthState.QUARANTINED: 5,
    HealthState.UNAVAILABLE: 6,
}


@dataclass(frozen=True)
class ComponentHealth:
    component: str
    state: HealthState
    reason: ReasonCode | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        payload = {"component": self.component, "state": self.state.value}
        if self.reason is not None:
            payload["reason_code"] = self.reason.value
        if self.detail:
            payload["detail"] = self.detail
        return payload


def healthy(component: str) -> ComponentHealth:
    return ComponentHealth(component, HealthState.HEALTHY)


def aggregate_health(components: list[ComponentHealth]) -> HealthState:
    """Overall health is the worst component health (never optimistic)."""
    if not components:
        return HealthState.UNKNOWN
    return max((item.state for item in components), key=lambda state: _SEVERITY[state])


def summarize(components: list[ComponentHealth]) -> dict[str, object]:
    return {
        "overall": aggregate_health(components).value,
        "components": [item.as_dict() for item in components],
    }


__all__ = [
    "ComponentHealth",
    "HealthState",
    "ReasonCode",
    "aggregate_health",
    "healthy",
    "summarize",
]
