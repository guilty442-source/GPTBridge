from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SystemBoundary:
    key: str
    role: str
    roots: tuple[str, ...]
    may_modify_mother_tool: bool
    may_modify_child_tools: bool
    may_run_tasks: bool


SYSTEM_BOUNDARIES: tuple[SystemBoundary, ...] = (
    SystemBoundary(
        key="interface_system",
        role="daily monitor and mode switching",
        roots=("src-ui/renderer/info-center",),
        may_modify_mother_tool=False,
        may_modify_child_tools=False,
        may_run_tasks=False,
    ),
    SystemBoundary(
        key="core_system",
        role="mother-tool core, shared services, architecture, and engines",
        roots=("src-core", "src-ui/renderer/core-system", "src-ui/renderer/components", "src-ui/renderer/hooks", "src-ui/renderer/types", "src-ui/renderer/shared"),
        may_modify_mother_tool=False,
        may_modify_child_tools=False,
        may_run_tasks=True,
    ),
    SystemBoundary(
        key="design_mode",
        role="child-tool development only",
        roots=("src-core/modes/design", "src-ui/renderer/modes/design"),
        may_modify_mother_tool=False,
        may_modify_child_tools=True,
        may_run_tasks=True,
    ),
    SystemBoundary(
        key="rescue_mode",
        role="mother-tool diagnosis, rescue, cleanup, and rollback",
        roots=("src-core/modes/rescue", "src-ui/renderer/modes/rescue"),
        may_modify_mother_tool=False,
        may_modify_child_tools=False,
        may_run_tasks=True,
    ),
    SystemBoundary(
        key="developer_mode",
        role="sandbox-only mother-tool development and deployment approval",
        roots=("src-core/modes/developer", "src-ui/renderer/modes/developer"),
        may_modify_mother_tool=True,
        may_modify_child_tools=False,
        may_run_tasks=True,
    ),
    SystemBoundary(
        key="settings",
        role="governance, configuration, maintenance, and cleanup",
        roots=("src-core/modes/settings", "src-ui/renderer/modes/settings", "config"),
        may_modify_mother_tool=False,
        may_modify_child_tools=False,
        may_run_tasks=True,
    ),
)

PROTECTED_ROOTS = (
    "src-core",
    "src-ui",
    "config",
    "runtime",
    "backups",
    ".GPTBridge_RuntimeSandbox",
    "node_modules",
    "release",
    "dist",
    "dist-ui",
)


def boundary_roots(project_root: Path, key: str) -> list[Path]:
    for boundary in SYSTEM_BOUNDARIES:
        if boundary.key == key:
            return [(project_root / rel_path).resolve() for rel_path in boundary.roots]
    return []


def boundary_manifest() -> list[dict[str, object]]:
    return [
        {
            "key": item.key,
            "role": item.role,
            "roots": list(item.roots),
            "may_modify_mother_tool": item.may_modify_mother_tool,
            "may_modify_child_tools": item.may_modify_child_tools,
            "may_run_tasks": item.may_run_tasks,
        }
        for item in SYSTEM_BOUNDARIES
    ]

