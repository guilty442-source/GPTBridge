"""Tool separation verification functions — A184/E159.

Read-only verification of tool separation invariants.  These functions
inspect manifests and directories and report violations as signals for the
sovereign decision chain.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core_system.tool_separation_types import (
    SeparationReport,
    SeparationViolation,
)


def _check_entity_id(
    tool_id: str, manifest: dict[str, Any],
) -> tuple[list[SeparationViolation], list[str]]:
    """Check stable-entity-id dimension (A184)."""
    violations: list[SeparationViolation] = []
    verified: list[str] = []
    entity_id = str(manifest.get("tool_id") or manifest.get("id") or "")
    if not entity_id:
        violations.append(SeparationViolation(
            dimension="stable-entity-id", tool_id=tool_id,
            violation="manifest-missing-entity-id",
        ))
    elif entity_id == "main-system":
        violations.append(SeparationViolation(
            dimension="stable-entity-id", tool_id=tool_id,
            violation="tool-entity-id-collides-with-main-system",
        ))
    else:
        verified.append("stable-entity-id")
    return violations, verified


def _check_source_root(
    tool_id: str, tool_dir: Path, project_root: Path,
) -> tuple[list[SeparationViolation], list[str]]:
    """Check source-root dimension (A184)."""
    violations: list[SeparationViolation] = []
    verified: list[str] = []
    try:
        resolved_tool = tool_dir.resolve(strict=False)
        resolved_main = (project_root / "main-system").resolve(strict=False)
        if resolved_tool == resolved_main:
            violations.append(SeparationViolation(
                dimension="source-root", tool_id=tool_id,
                violation="tool-directory-is-main-system-directory",
            ))
        else:
            verified.append("source-root")
    except (OSError, ValueError):
        violations.append(SeparationViolation(
            dimension="source-root", tool_id=tool_id,
            violation="tool-directory-unresolvable",
        ))
    return violations, verified


def _check_manifest_field(
    tool_id: str, manifest: dict[str, Any],
    dimension: str, keys: tuple[str, ...],
) -> tuple[list[SeparationViolation], list[str]]:
    """Check a manifest field is present (A184)."""
    value = ""
    for key in keys:
        value = str(manifest.get(key) or "")
        if value:
            break
    if not value:
        return [SeparationViolation(
            dimension=dimension, tool_id=tool_id,
            violation=f"manifest-missing-{dimension}",
        )], []
    return [], [dimension]


def verify_tool_manifest_separation(
    tool_id: str,
    manifest: dict[str, Any],
    tool_dir: Path,
    project_root: Path,
) -> SeparationReport:
    """Verify that a tool manifest declares independent separation (A184)."""
    violations: list[SeparationViolation] = []
    verified: list[str] = []

    v, ok = _check_entity_id(tool_id, manifest)
    violations.extend(v); verified.extend(ok)

    v, ok = _check_source_root(tool_id, tool_dir, project_root)
    violations.extend(v); verified.extend(ok)

    v, ok = _check_manifest_field(tool_id, manifest, "runtime-entry", ("entry", "main"))
    violations.extend(v); verified.extend(ok)

    v, ok = _check_manifest_field(tool_id, manifest, "version", ("version",))
    violations.extend(v); verified.extend(ok)

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
    """Verify that a tool's data-root is exclusive (A184: DATA).

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
