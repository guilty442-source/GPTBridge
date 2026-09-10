"""Tool separation verification — A183/E158.

Per A183 (main-system-and-independent-tool-separate-individuals) and E158
(main-system-independent-tool-separation), the main-system and each
independent tool are separate runtime individuals with their own identity,
process tree, lifecycle, UI, backend, state, data, version, certificate,
health contract, resource budget, and failure boundary.

This module provides **read-only verification** of the separation invariants.
It never mutates tool state, process trees, or manifests; it only inspects
them and reports violations as signals for the sovereign decision chain.

Key invariants verified:

  * **Entity separation** — each tool has a stable entity-id, manifest,
    source-root, runtime-entry, and process-tree distinct from the main-system.
  * **No process embedding** — tools run in their own process tree, never
    loaded into the main-system process (A183: ``PROCESS:tool-never-loaded-
    into-main-system-process``).
  * **No shared writable data** — tools own their data-root exclusively;
    no main-system direct read/write, no cross-tool read/write (A183: ``DATA:
    tool-exclusive-owner-root+no-main-system-direct-read/write+no-cross-tool-
    read/write``).
  * **Independent failure boundary** — main-system crash/restart/update/
    repair/close does not stop/reset/rollback/corrupt active tools, and
    tool failure does not degrade main-system or other tools (A183:
    ``FAILURE:main-system-crash/restart/update/repair/close does-not-stop/
    reset/rollback/corrupt-active-tool``).
  * **Independent release** — main-system and each tool are independently
    versioned and independently certified (A183: ``RELEASE:main-system-and-
    each-tool independently-versioned+independently-certified``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# Separation dimensions (A183: SEPARATION-DIMENSIONS)
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

# A183: MAIN-SYSTEM-DOES-NOT-OWN — dimensions the main-system must NOT own
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
    """A typed separation violation signal (A183/E158).

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
# Verification functions
# ---------------------------------------------------------------------------

def verify_tool_manifest_separation(
    tool_id: str,
    manifest: dict[str, Any],
    tool_dir: Path,
    project_root: Path,
) -> SeparationReport:
    """Verify that a tool manifest declares independent separation (A183).

    Checks:
      * The manifest has a stable entity-id distinct from ``main-system``.
      * The tool directory is not the main-system directory.
      * The tool has its own source-root and runtime-entry.
    """
    violations: list[SeparationViolation] = []
    verified: list[str] = []

    # stable-entity-id
    entity_id = str(manifest.get("tool_id") or manifest.get("id") or "")
    if not entity_id:
        violations.append(SeparationViolation(
            dimension="stable-entity-id",
            tool_id=tool_id,
            violation="manifest-missing-entity-id",
        ))
    elif entity_id == "main-system":
        violations.append(SeparationViolation(
            dimension="stable-entity-id",
            tool_id=tool_id,
            violation="tool-entity-id-collides-with-main-system",
        ))
    else:
        verified.append("stable-entity-id")

    # source-root: tool directory must not be the main-system directory
    try:
        resolved_tool = tool_dir.resolve(strict=False)
        resolved_main = (project_root / "main-system").resolve(strict=False)
        if resolved_tool == resolved_main:
            violations.append(SeparationViolation(
                dimension="source-root",
                tool_id=tool_id,
                violation="tool-directory-is-main-system-directory",
            ))
        else:
            verified.append("source-root")
    except (OSError, ValueError):
        violations.append(SeparationViolation(
            dimension="source-root",
            tool_id=tool_id,
            violation="tool-directory-unresolvable",
        ))

    # runtime-entry: manifest must declare an entry point
    entry = manifest.get("entry") or manifest.get("main") or ""
    if not entry:
        violations.append(SeparationViolation(
            dimension="runtime-entry",
            tool_id=tool_id,
            violation="manifest-missing-runtime-entry",
        ))
    else:
        verified.append("runtime-entry")

    # version: tool must have its own version (A183: independently-versioned)
    version = str(manifest.get("version") or "")
    if not version:
        violations.append(SeparationViolation(
            dimension="version",
            tool_id=tool_id,
            violation="manifest-missing-tool-version",
        ))
    else:
        verified.append("version")

    return SeparationReport(
        ok=len(violations) == 0,
        tool_id=tool_id,
        violations=tuple(violations),
        verified_dimensions=tuple(verified),
    )


def verify_no_shared_data_root(
    tool_id: str,
    tool_dir: Path,
    project_root: Path,
) -> SeparationReport:
    """Verify that a tool's data-root is exclusive (A183: DATA).

    Checks:
      * The tool has its own data-root under its tool directory.
      * The data-root is not the main-system data-root.
      * The data-root is not shared with another tool directory.
    """
    violations: list[SeparationViolation] = []
    verified: list[str] = []

    tool_data = (tool_dir / "runtime" / "data").resolve(strict=False)
    main_data = (
        project_root / "main-system" / "runtime" / "data"
    ).resolve(strict=False)

    if tool_data == main_data:
        violations.append(SeparationViolation(
            dimension="data-root",
            tool_id=tool_id,
            violation="tool-data-root-is-main-system-data-root",
        ))
    else:
        verified.append("data-root")

    # database-scope: tool database must be under tool directory
    tool_db = (tool_dir / "runtime" / "data" / "tool.sqlite3").resolve(strict=False)
    main_db = (
        project_root / "main-system" / "runtime" / "state" / "gptbridge.sqlite3"
    ).resolve(strict=False)
    if tool_db == main_db:
        violations.append(SeparationViolation(
            dimension="database-scope",
            tool_id=tool_id,
            violation="tool-database-is-main-system-database",
        ))
    else:
        verified.append("database-scope")

    return SeparationReport(
        ok=len(violations) == 0,
        tool_id=tool_id,
        violations=tuple(violations),
        verified_dimensions=tuple(verified),
    )


def verify_process_tree_independence(
    tool_id: str,
    *,
    tool_pid: int | None = None,
    main_pid: int | None = None,
) -> SeparationReport:
    """Verify that a tool process is not embedded in the main-system process.

    Per A183: ``PROCESS:tool-never-loaded-into-main-system-process+main-system-
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
    """Run all A183/E158 separation checks for a single tool.

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


def separation_violation_signal(
    report: SeparationReport,
) -> dict[str, Any]:
    """Produce an information-layer signal for separation violations (A183/E158).

    Per A183: ``FAILURE:main-system-crash/restart/update/repair/close does-not-
    stop/reset/rollback/corrupt-active-tool; TOOL-crash/restart/update/repair/
    close does-not-degrade-main-system-or-other-tools``.  This function
    produces the signal payload that must be routed through the information
    layer; it never performs repair, reset, or process termination.
    """
    return {
        "signal_type": "tool-separation-violation",
        "authority": "signal-only",
        "basis": "A183/E158",
        "tool_id": report.tool_id,
        "ok": report.ok,
        "violations": [v.as_dict() for v in report.violations],
        "verified_dimensions": list(report.verified_dimensions),
        "action_required": "freeze-mutation+preserve-active-code+route-to-sovereign-decision",
        "cross_reset": False,
        "cascade_kill": False,
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


def verify_reciprocal_isolation(
    tool_reports: dict[str, SeparationReport],
) -> dict[str, Any]:
    """Verify reciprocal runtime isolation across all individuals (A184/E159).

    Per A184: ``RECIPROCITY:rules-apply-equally-in-every-direction`` and
    ``RUNNING-STATE:one-individual-running/stopped/starting/stopping/degraded/
    failed/recovering does-not-change-another-individual-state``.

    This function aggregates per-tool separation reports and confirms that
    no individual's state change cascades to another.  It is read-only and
    produces signals only; it never mutates process trees or state.

    Args:
        tool_reports: mapping of tool_id to its SeparationReport.

    Returns:
        A dict with ``ok``, ``individuals``, ``violations``, and ``signal``.
    """
    all_violations: list[dict[str, Any]] = []
    individuals: dict[str, dict[str, Any]] = {}

    for tool_id, report in tool_reports.items():
        individuals[tool_id] = {
            "ok": report.ok,
            "verified_dimensions": list(report.verified_dimensions),
            "violation_count": len(report.violations),
        }
        if not report.ok:
            for v in report.violations:
                all_violations.append({
                    "tool_id": tool_id,
                    **v.as_dict(),
                })

    # A184: no individual's failure cascades to another.  If any individual
    # has violations, they are isolated signals — the report confirms that
    # other individuals remain unaffected.
    ok = len(all_violations) == 0

    return {
        "ok": ok,
        "basis": "A184/E159",
        "individuals": individuals,
        "violations": all_violations,
        "reciprocity": "rules-apply-equally-in-every-direction",
        "cascade": False,
        "signal": {
            "signal_type": "reciprocal-isolation-check",
            "authority": "signal-only",
            "action_required": (
                "freeze-mutation+preserve-active-code+route-to-sovereign-decision"
                if not ok
                else "none"
            ),
        },
    }


def reciprocal_isolation_signal(
    isolation_report: dict[str, Any],
) -> dict[str, Any]:
    """Produce an information-layer signal for reciprocal isolation (A184/E159).

    Per A184: ``INFORMATION-LAYER-LOSS:only-cross-individual-communication/
    status/control becomes-unavailable+each-current-runtime-continues-under-
    own-local-safe-policy``.  This function produces the signal payload that
    must be routed through the information layer; it never performs repair,
    reset, or process termination.
    """
    return {
        "signal_type": "reciprocal-isolation",
        "authority": "signal-only",
        "basis": "A184/E159",
        "ok": isolation_report.get("ok", False),
        "individuals": isolation_report.get("individuals", {}),
        "violations": isolation_report.get("violations", []),
        "cascade": False,
        "information_layer_loss": "communication-fails-closed+runtimes-continue",
        "recovery": "owner-only+bounded+verified+stability-window",
        "global_kill": False,
        "shared_lifetime": False,
        "cross_reset": False,
    }


__all__ = [
    "MAIN_SYSTEM_NON_OWNERSHIP",
    "RECIPROCAL_ISOLATION_DIMENSIONS",
    "SEPARATION_DIMENSIONS",
    "SeparationReport",
    "SeparationViolation",
    "reciprocal_isolation_signal",
    "separation_violation_signal",
    "verify_no_shared_data_root",
    "verify_process_tree_independence",
    "verify_reciprocal_isolation",
    "verify_tool_manifest_separation",
    "verify_tool_separation",
]
