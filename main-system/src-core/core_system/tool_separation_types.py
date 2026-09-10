"""Tool separation types and constants — A184/E159.

Type and constant definitions for tool separation verification.
This module has no imports from verification or signal submodules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# Separation dimensions (A184: SEPARATION-DIMENSIONS)
# ---------------------------------------------------------------------------

SEPARATION_DIMENSIONS: Final[tuple[str, ...]] = (
    "stable-entity-id",
    "manifest",
    "source-root",
    "runtime-entry",
    "process-tree",
    "frontend-window",
    "backend-runtime",
    "session",
    "release-id",
    "version",
    "certificate",
    "health-contract",
    "resource-budget",
    "data-root",
    "database-scope",
    "logs",
    "cache",
    "temporary-files",
    "backup",
    "repair-history",
    "shutdown-token",
)

# A184: MAIN-SYSTEM-DOES-NOT-OWN — dimensions the main-system must NOT own
# for any tool.
MAIN_SYSTEM_NON_OWNERSHIP: Final[tuple[str, ...]] = (
    "tool-business-logic",
    "tool-runtime-state",
    "tool-data",
    "tool-database",
    "tool-version",
    "tool-release",
    "tool-health-verdict",
    "tool-repair-content",
)


# ---------------------------------------------------------------------------
# Verification result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SeparationViolation:
    """A typed separation violation signal (A184/E159).

    This is a **signal only**; it carries no mutation authority.  The caller
    must route it through the information layer to the sovereign decision
    chain.
    """

    dimension: str
    tool_id: str
    violation: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SeparationReport:
    """Result of verifying tool separation invariants."""

    ok: bool
    tool_id: str
    violations: tuple[SeparationViolation, ...] = ()
    verified_dimensions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "tool_id": self.tool_id,
            "violations": [v.as_dict() for v in self.violations],
            "verified_dimensions": list(self.verified_dimensions),
        }


# ---------------------------------------------------------------------------
# Reciprocal runtime isolation (A184/E159)
# ---------------------------------------------------------------------------

# A184: EACH-INDIVIDUAL-OWNS — dimensions each individual owns exclusively.
RECIPROCAL_ISOLATION_DIMENSIONS: Final[tuple[str, ...]] = (
    "process-tree",
    "os-lifecycle-boundary",
    "runtime-generation",
    "supervisor",
    "watchdog",
    "frontend",
    "backend",
    "threads",
    "tasks",
    "queues",
    "connections",
    "resource-budget",
    "state",
    "data",
    "logs",
    "cache",
    "temporary-files",
    "release",
    "certificate",
    "health",
    "repair",
    "update",
    "shutdown",
)
