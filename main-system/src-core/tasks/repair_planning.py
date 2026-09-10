from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final

from core_system.versioning import component_version

CENTRAL_REPAIR_VERSION: Final[str] = component_version("central-repair")

SOURCE_SELF_REPAIR_FAILURES: Final[frozenset[str]] = frozenset(
    {
        "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED",
        "BACKEND_CONNECTION_FAILED",
        "FRONTEND_BACKEND_DISCONNECTED",
        "PROCESS_START_FAILED",
    }
)

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
    failure_code: str
    inspect_databases: bool
    rebuild_executable: bool
    repair_main_system_source: bool = False

    @property
    def actions(self) -> tuple[str, ...]:
        actions = ("inspect-owned-databases",) if self.inspect_databases else ()
        if self.rebuild_executable:
            actions = (*actions, "rebuild-tool-executable")
        if self.repair_main_system_source:
            actions = (*actions, "repair-main-system-source")
        return actions

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "actions": list(self.actions)}


def plan_repair(failure_code: str) -> RepairPlan:
    normalized = str(failure_code or "TOOL_START_FAILED").strip().upper()[:128]
    return RepairPlan(
        failure_code=normalized or "TOOL_START_FAILED",
        inspect_databases=True,
        rebuild_executable=normalized in PACKAGE_REBUILD_FAILURES,
        repair_main_system_source=normalized in SOURCE_SELF_REPAIR_FAILURES,
    )


REPAIR_RECIPES: Final[tuple[dict[str, Any], ...]] = (
    {
        "recipe_id": "main-system-python-source-syntax",
        "name": "Backend Python source indentation/syntax self-repair",
        "failure_signatures": (
            "MAIN_SYSTEM_SOURCE_SYNTAX_FAILED",
            "IndentationError",
            "TabError",
            "SyntaxError",
        ),
        "owner": "main-system",
        "remedy": (
            "compile self-check over all backend src roots then deterministic "
            "column-0 indentation recovery via tasks.source_repair "
            "(skips files with uncommitted git changes)"
        ),
        "verification": (
            "full source compile passes across all backend src roots; "
            "repair recorded in automatic-repair store; files with "
            "uncommitted git changes are skipped"
        ),
        "automatic": True,
        "runtime_only": False,
    },
    {
        "recipe_id": "tool-package-rebuild",
        "name": "Tool package rebuild on startup/runtime failure",
        "failure_signatures": tuple(sorted(PACKAGE_REBUILD_FAILURES)),
        "owner": "main-system",
        "remedy": "plan_repair rebuild-tool-executable via governed package rebuilder",
        "verification": "owned databases inspected; rebuilt executable starts",
        "automatic": True,
        "runtime_only": True,
    },
    {
        "recipe_id": "backend-exit-before-health",
        "name": "Backend exits before health ready",
        "failure_signatures": ("backend exited before health ready",),
        "owner": "main-system",
        "remedy": "governance-authorized re-spawn with bounded startup/autonomous recovery in launcher",
        "verification": "health probe returns ready with matching workspace instance id",
        "automatic": True,
        "runtime_only": True,
    },
    {
        "recipe_id": "frontend-backend-disconnected",
        "name": "Frontend-backend WebSocket disconnection repair",
        "failure_signatures": ("FRONTEND_BACKEND_DISCONNECTED",),
        "owner": "main-system",
        "remedy": (
            "connection watchdog detects persistent disconnection; "
            "frontend auto-reconnects (3 attempts) then triggers "
            "app:restart-backend via Electron IPC; boot_core restarts "
            "backend; learning store records the outage pattern"
        ),
        "verification": (
            "backend /health returns 200 and frontend WebSocket "
            "reconnects within probe interval"
        ),
        "automatic": True,
        "runtime_only": True,
    },
    {
        "recipe_id": "governance-codex-tamper",
        "name": "Governance codex file tamper detection",
        "failure_signatures": ("PermissionError", "AuthorityIntegrityGuard.verify"),
        "owner": "governance-rule",
        "remedy": (
            "fail-closed denial; restore codex files from trusted backup and "
            "re-verify manifest"
        ),
        "verification": "governance:audit and runtime integrity report healthy",
        "automatic": False,
        "runtime_only": False,
    },
)
