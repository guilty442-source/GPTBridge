from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Final, Iterable

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

# Action tokens that map a recipe's ``actions`` list to the structured
# booleans on RepairPlan.  Keeping this mapping in one place lets new
# recipes (including learned ones) drive repair behaviour without edits
# to the planning code.
_ACTION_INSPECT_DATABASES: Final[str] = "inspect-owned-databases"
_ACTION_REBUILD_EXECUTABLE: Final[str] = "rebuild-tool-executable"
_ACTION_REPAIR_SOURCE: Final[str] = "repair-main-system-source"

_ALL_ACTIONS: Final[frozenset[str]] = frozenset(
    {_ACTION_INSPECT_DATABASES, _ACTION_REBUILD_EXECUTABLE, _ACTION_REPAIR_SOURCE}
)


@dataclass(frozen=True)
class RepairPlan:
    failure_code: str
    inspect_databases: bool
    rebuild_executable: bool
    repair_main_system_source: bool = False
    recipe_id: str = ""
    remedy: str = ""
    source: str = "static"

    @property
    def actions(self) -> tuple[str, ...]:
        actions = (_ACTION_INSPECT_DATABASES,) if self.inspect_databases else ()
        if self.rebuild_executable:
            actions = (*actions, _ACTION_REBUILD_EXECUTABLE)
        if self.repair_main_system_source:
            actions = (*actions, _ACTION_REPAIR_SOURCE)
        return actions

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "actions": list(self.actions)}


def _signature_matches(failure_code: str, signatures: Iterable[str]) -> bool:
    """Return True if ``failure_code`` matches any entry in ``signatures``.

    Matching is case-insensitive substring-aware: an exact match wins, but
    a signature that is a substring of the failure code (or vice-versa) also
    matches so that learned recipes with partial signatures still fire.
    """
    needle = failure_code.strip().upper()
    if not needle:
        return False
    for sig in signatures:
        hay = str(sig).strip().upper()
        if not hay:
            continue
        if needle == hay or hay in needle or needle in hay:
            return True
    return False


def _plan_from_recipe(failure_code: str, recipe: dict[str, Any]) -> RepairPlan:
    """Build a RepairPlan dynamically from a matched recipe."""
    raw_actions = recipe.get("actions")
    if not isinstance(raw_actions, (list, tuple)):
        raw_actions = ()
    action_set = {str(a).strip() for a in raw_actions if str(a).strip()}

    inspect_databases = _ACTION_INSPECT_DATABASES in action_set or not action_set
    rebuild_executable = _ACTION_REBUILD_EXECUTABLE in action_set
    repair_source = _ACTION_REPAIR_SOURCE in action_set

    return RepairPlan(
        failure_code=failure_code,
        inspect_databases=inspect_databases,
        rebuild_executable=rebuild_executable,
        repair_main_system_source=repair_source,
        recipe_id=str(recipe.get("recipe_id") or ""),
        remedy=str(recipe.get("remedy") or ""),
        source=str(recipe.get("source") or "static"),
    )


def _fallback_plan(failure_code: str) -> RepairPlan:
    """Backwards-compatible static plan used when no recipe matches.

    This preserves the original hard-coded frozenset behaviour so existing
    deployments that have not yet populated recipe ``actions`` continue to
    work unchanged.
    """
    normalized = str(failure_code or "TOOL_START_FAILED").strip().upper()[:128]
    code = normalized or "TOOL_START_FAILED"
    return RepairPlan(
        failure_code=code,
        inspect_databases=True,
        rebuild_executable=code in PACKAGE_REBUILD_FAILURES,
        repair_main_system_source=code in SOURCE_SELF_REPAIR_FAILURES,
        recipe_id="",
        remedy="",
        source="fallback",
    )


def plan_repair(
    failure_code: str,
    *,
    recipes: Iterable[dict[str, Any]] | None = None,
) -> RepairPlan:
    """Plan a repair for ``failure_code``.

    When ``recipes`` is provided (typically from ``CentralRepairService.
    known_recipes()``), the planner dynamically matches the failure code
    against each recipe's ``failure_signatures`` and builds the plan from
    the first matching recipe's ``actions`` list.  This makes the repair
    strategy data-driven: adding a new recipe (static or learned) is
    sufficient to teach the system a new repair without code changes.

    When no recipe matches, or when ``recipes`` is omitted, the planner
    falls back to the original static frozenset logic so behaviour is
    preserved for callers that have not been migrated.
    """
    normalized = str(failure_code or "TOOL_START_FAILED").strip().upper()[:128]
    code = normalized or "TOOL_START_FAILED"

    if recipes is not None:
        for recipe in recipes:
            if not isinstance(recipe, dict):
                continue
            # Only automatic recipes drive repair; manual ones (e.g.
            # codex-tamper) require human approval and are skipped here.
            if recipe.get("automatic") is not True:
                continue
            signatures = recipe.get("failure_signatures")
            if not isinstance(signatures, (list, tuple)):
                continue
            if _signature_matches(code, signatures):
                return _plan_from_recipe(code, recipe)

    return _fallback_plan(code)


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
        "actions": (
            "inspect-owned-databases",
            "repair-main-system-source",
        ),
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
        "actions": (
            "inspect-owned-databases",
            "rebuild-tool-executable",
        ),
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
        "actions": (
            "inspect-owned-databases",
            "rebuild-tool-executable",
        ),
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
        "actions": (
            "inspect-owned-databases",
            "rebuild-tool-executable",
            "repair-main-system-source",
        ),
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
        "recipe_id": "codex-tamper",
        "name": "Governance codex file tamper detection",
        "failure_signatures": ("PermissionError", "AuthorityIntegrityGuard.verify"),
        "owner": "governance-rule",
        "actions": (),  # manual — requires human approval
        "remedy": (
            "fail-closed denial; restore codex files from trusted backup and "
            "re-verify manifest"
        ),
        "verification": "governance:audit and runtime integrity report healthy",
        "automatic": False,
        "runtime_only": False,
    },
)
