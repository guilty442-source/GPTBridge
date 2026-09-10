"""Source size report dataclasses — A185/E160.

SizeViolation and SizeReport dataclasses for source size verification
results.  This module imports from the types module only (no circular
imports).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core_system.source_size_types import SourceSizeMeasurement


@dataclass(frozen=True)
class SizeViolation:
    """A typed source size violation signal (A185/E160)."""

    dimension: str
    path: str
    measured: int
    limit: int
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SizeReport:
    """Result of verifying source size limits for a file."""

    ok: bool
    path: str
    measurement: SourceSizeMeasurement | None
    violations: tuple[SizeViolation, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "measurement": self.measurement.as_dict() if self.measurement else None,
            "violations": [v.as_dict() for v in self.violations],
            "warnings": list(self.warnings),
        }
