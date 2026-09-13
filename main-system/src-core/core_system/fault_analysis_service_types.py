"""Fault analysis service — data structures and constants.

Provides the FaultSummary and FaultPattern dataclasses and the
path constants used by the fault analysis service.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------

_AUTOMATIC_REPAIR_ROOT: Final[tuple[str, ...]] = (
    "main-system", "data", "automatic-repair",
)
_RUNTIME_STATE_ROOT: Final[tuple[str, ...]] = (
    "main-system", "runtime", "state",
)
_SYSTEM_RESCUE_ROOT: Final[tuple[str, ...]] = (
    "system-rescue", "data",
)
_QUARANTINE_DIR: Final[tuple[str, ...]] = (
    "main-system", "runtime", "state", "tool-crash-quarantine",
)


@dataclass
class FaultSummary:
    """Aggregated fault summary for a single fault incident."""
    fault_id: str
    fault_type: str  # crash | repair | health | quarantine | audit
    source: str  # boot-core | tool:{id} | system-rescue | ...
    timestamp: str
    severity: str  # critical | high | medium | low | info
    error_class: str
    error_message: str
    target_entity: str
    repair_action: str
    repair_outcome: str  # success | failure | pending | skipped | none
    raw_evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fault_id": self.fault_id,
            "fault_type": self.fault_type,
            "source": self.source,
            "timestamp": self.timestamp,
            "severity": self.severity,
            "error_class": self.error_class,
            "error_message": self.error_message,
            "target_entity": self.target_entity,
            "repair_action": self.repair_action,
            "repair_outcome": self.repair_outcome,
            "raw_evidence": self.raw_evidence,
        }


@dataclass
class FaultPattern:
    """A recurring fault pattern detected across multiple incidents."""
    pattern_id: str
    error_signature: str
    error_class: str
    occurrence_count: int
    first_seen: str
    last_seen: str
    affected_entities: list[str]
    common_repair_action: str
    success_rate: float  # 0.0 to 1.0
    severity_trend: str  # increasing | stable | decreasing | unknown
    sample_fault_ids: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "error_signature": self.error_signature,
            "error_class": self.error_class,
            "occurrence_count": self.occurrence_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "affected_entities": self.affected_entities,
            "common_repair_action": self.common_repair_action,
            "success_rate": round(self.success_rate, 3),
            "severity_trend": self.severity_trend,
            "sample_fault_ids": self.sample_fault_ids,
        }


__all__ = [
    "_iso_now",
    "_AUTOMATIC_REPAIR_ROOT",
    "_RUNTIME_STATE_ROOT",
    "_SYSTEM_RESCUE_ROOT",
    "_QUARANTINE_DIR",
    "FaultSummary",
    "FaultPattern",
]
