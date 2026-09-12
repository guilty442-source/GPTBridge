"""Startup Sovereign executor — result types (split for A185/E160 size).

Dataclasses for per-phase evidence and generation results, split from
``startup_executor`` to keep each source file under 500 lines.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PhaseRecord:
    """Evidence for one executed startup phase."""

    phase_id: str
    budget_ms: int
    ok: bool = False
    skipped: bool = False
    duration_ms: int = 0
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StartupResult:
    """Outcome of one startup generation (E155 generation-fenced)."""

    generation_id: str
    ok: bool
    phases: list[PhaseRecord] = field(default_factory=list)
    elapsed_ms: int = 0
    deadline_ms: int = 0
    bottleneck: str = ""
    failure_phase: str = ""
    handoff: dict[str, Any] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation_id": self.generation_id,
            "ok": self.ok,
            "phases": [p.as_dict() for p in self.phases],
            "elapsed_ms": self.elapsed_ms,
            "deadline_ms": self.deadline_ms,
            "bottleneck": self.bottleneck,
            "failure_phase": self.failure_phase,
            "handoff": self.handoff,
            "violations": self.violations,
        }


__all__ = ["PhaseRecord", "StartupResult"]
