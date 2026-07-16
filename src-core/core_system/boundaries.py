from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SystemBoundary:
    key: str
    role: str
    roots: tuple[str, ...]
    may_modify_main_program: bool
    may_modify_independent_tools: bool
    may_run_tasks: bool


SYSTEM_BOUNDARIES: tuple[SystemBoundary, ...] = (
    SystemBoundary(
        key="main_program",
        role="launcher, status, IPC broker, governance, and tool lifecycle",
        roots=("src-core", "src-ui", "config"),
        may_modify_main_program=False,
        may_modify_independent_tools=False,
        may_run_tasks=True,
    ),
    SystemBoundary(
        key="independent_tools",
        role="standalone applications that own their UI, runtime, data, and capabilities",
        roots=("platform_tools",),
        may_modify_main_program=False,
        may_modify_independent_tools=True,
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
            "may_modify_main_program": item.may_modify_main_program,
            "may_modify_independent_tools": item.may_modify_independent_tools,
            "may_run_tasks": item.may_run_tasks,
        }
        for item in SYSTEM_BOUNDARIES
    ]
