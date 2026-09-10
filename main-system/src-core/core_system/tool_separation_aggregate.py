"""Tool separation aggregate verification — A184/E159.

Combines per-tool checks into overall separation reports, including
process-tree independence and the full single-tool separation check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core_system.tool_separation_types import (
    SeparationReport,
    SeparationViolation,
)
from core_system.tool_separation_verify import (
    verify_no_shared_data_root,
    verify_tool_manifest_separation,
)


def verify_process_tree_independence(
    tool_id: str,
    *,
    tool_pid: int | None = None,
    main_pid: int | None = None,
) -> SeparationReport:
    """Verify that a tool process is not embedded in the main-system process.

    Per A184: ``PROCESS:tool-never-loaded-into-main-system-process+main-system-
    never-loaded-into-tool-process``.  This check confirms the tool PID is not
    the same as the main-system PID (a basic identity check; full process-tree
    verification requires platform-specific APIs).
    """
    violations: list[SeparationViolation] = []
    verified: list[str] = []

    if tool_pid is not None and main_pid is not None:
        if tool_pid == main_pid:
            violations.append(SeparationViolation(
                dimension="process-tree",
                tool_id=tool_id,
                violation="tool-pid-is-main-system-pid",
                evidence={"tool_pid": tool_pid, "main_pid": main_pid},
            ))
        else:
            verified.append("process-tree")
    else:
        # Cannot verify without PIDs; report as unverified (not a violation).
        verified.append("process-tree-unverified")

    return SeparationReport(
        ok=len(violations) == 0,
        tool_id=tool_id,
        violations=tuple(violations),
        verified_dimensions=tuple(verified),
    )


def verify_tool_separation(
    tool_id: str,
    manifest: dict[str, Any],
    tool_dir: Path,
    project_root: Path,
    *,
    tool_pid: int | None = None,
    main_pid: int | None = None,
) -> SeparationReport:
    """Run all A184/E159 separation checks for a single tool.

    Combines manifest separation, data-root exclusivity, and process-tree
    independence into a single report.
    """
    manifest_report = verify_tool_manifest_separation(
        tool_id, manifest, tool_dir, project_root
    )
    data_report = verify_no_shared_data_root(tool_id, tool_dir, project_root)
    process_report = verify_process_tree_independence(
        tool_id, tool_pid=tool_pid, main_pid=main_pid
    )

    all_violations = (
        manifest_report.violations
        + data_report.violations
        + process_report.violations
    )
    all_verified = (
        manifest_report.verified_dimensions
        + data_report.verified_dimensions
        + process_report.verified_dimensions
    )

    return SeparationReport(
        ok=len(all_violations) == 0,
        tool_id=tool_id,
        violations=tuple(all_violations),
        verified_dimensions=tuple(all_verified),
    )
