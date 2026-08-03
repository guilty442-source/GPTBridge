from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final


PACKAGE_REBUILD_FAILURES: Final[frozenset[str]] = frozenset(
    {
        "EXECUTABLE_MISSING",
        "PACKAGE_UNVERIFIED",
        "STALE_TOOL_PACKAGE",
        "INCOMPATIBLE_TOOL_RUNTIME",
        "PROCESS_START_FAILED",
        "SOURCE_UI_UNAVAILABLE",
        "SOURCE_RUNTIME_NOT_READY",
        "TOOL_VERSION_MISMATCH",
        "BACKEND_CONNECTION_FAILED",
        "FRONTEND_BACKEND_DISCONNECTED",
        "MODEL_RUNTIME_NOT_READY",
        "COMMAND_EXECUTION_FAILED",
        "STREAM_CHANNEL_FAILED",
    }
)


@dataclass(frozen=True)
class RepairPlan:
    """A fixed stability-only plan issued by the System Rescue owner."""

    failure_code: str
    inspect_databases: bool
    rebuild_executable: bool

    @property
    def actions(self) -> tuple[str, ...]:
        actions = ["inspect-owned-databases"] if self.inspect_databases else []
        if self.rebuild_executable:
            actions.append("rebuild-tool-executable")
        return tuple(actions)

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "actions": list(self.actions)}


def plan_repair(failure_code: str) -> RepairPlan:
    normalized = str(failure_code or "TOOL_START_FAILED").strip().upper()[:128]
    return RepairPlan(
        failure_code=normalized or "TOOL_START_FAILED",
        inspect_databases=True,
        rebuild_executable=normalized in PACKAGE_REBUILD_FAILURES,
    )


__all__ = ["PACKAGE_REBUILD_FAILURES", "RepairPlan", "plan_repair"]
