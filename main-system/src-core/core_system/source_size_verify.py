"""Source size verification functions — A185/E160.

Verifies source files and directories against A185/E160 size limits and
builds typed violation reports.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core_system.source_size_measure import measure_source
from core_system.source_size_report import (
    SizeReport,
    SizeViolation,
)
from core_system.source_size_types import (
    CALLABLES_LIMIT,
    CLASS_LINE_LIMIT,
    FUNCTION_LINE_LIMIT,
    MODULE_LINE_LIMIT,
    PUBLIC_ENTRY_LIMIT,
    SOURCE_FILE_EXTENSIONS,
    SourceSizeMeasurement,
)


def _build_size_violations(
    path: Path, m: SourceSizeMeasurement,
) -> list[SizeViolation]:
    """Build violation list from a measurement (A185/E160)."""
    violations: list[SizeViolation] = []
    if m.module_exceeds:
        violations.append(SizeViolation(
            dimension="module", path=str(path),
            measured=m.module_effective_lines, limit=m.module_limit,
            detail=f"module exceeds {m.module_limit} effective lines",
        ))
    if m.function_exceeds:
        violations.append(SizeViolation(
            dimension="function", path=str(path),
            measured=m.largest_function_lines, limit=m.function_limit,
            detail=f"function {m.largest_function_name} exceeds {m.function_limit} lines",
        ))
    if m.class_exceeds:
        violations.append(SizeViolation(
            dimension="class", path=str(path),
            measured=m.largest_class_lines, limit=m.class_limit,
            detail=f"class exceeds {m.class_limit} lines",
        ))
    if m.entry_exceeds:
        violations.append(SizeViolation(
            dimension="public-entrypoints", path=str(path),
            measured=m.public_entrypoints, limit=m.entry_limit,
            detail=f"public entrypoints exceed {m.entry_limit}",
        ))
    if m.callables_exceed:
        violations.append(SizeViolation(
            dimension="callables", path=str(path),
            measured=m.authored_callables, limit=m.callables_limit,
            detail=f"authored callables exceed {m.callables_limit}",
        ))
    return violations


def verify_source_size(
    path: Path,
    *,
    module_limit: int = MODULE_LINE_LIMIT,
    function_limit: int = FUNCTION_LINE_LIMIT,
    class_limit: int = CLASS_LINE_LIMIT,
    entry_limit: int = PUBLIC_ENTRY_LIMIT,
    callables_limit: int = CALLABLES_LIMIT,
) -> SizeReport:
    """Verify a source file against A185/E160 size limits."""
    measurement = measure_source(
        path,
        module_limit=module_limit,
        function_limit=function_limit,
        class_limit=class_limit,
        entry_limit=entry_limit,
        callables_limit=callables_limit,
    )
    if measurement is None:
        return SizeReport(
            ok=True, path=str(path),
            measurement=None, violations=(), warnings=(),
        )
    violations = _build_size_violations(path, measurement)
    return SizeReport(
        ok=len(violations) == 0,
        path=str(path),
        measurement=measurement,
        violations=tuple(violations),
        warnings=tuple(measurement.warnings),
    )


def verify_source_directory(
    root: Path,
    *,
    module_limit: int = MODULE_LINE_LIMIT,
    function_limit: int = FUNCTION_LINE_LIMIT,
    class_limit: int = CLASS_LINE_LIMIT,
    entry_limit: int = PUBLIC_ENTRY_LIMIT,
    callables_limit: int = CALLABLES_LIMIT,
) -> dict[str, Any]:
    """Verify all source files in a directory tree against A185/E160 limits.

    Returns a dict with ``ok``, ``checked``, ``violations``, ``warnings``,
    and ``basis``.
    """
    reports: list[SizeReport] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in SOURCE_FILE_EXTENSIONS:
            continue
        if "_pycache_" in str(path) or "node_modules" in str(path):
            continue
        report = verify_source_size(
            path,
            module_limit=module_limit,
            function_limit=function_limit,
            class_limit=class_limit,
            entry_limit=entry_limit,
            callables_limit=callables_limit,
        )
        reports.append(report)

    all_violations: list[dict[str, Any]] = []
    all_warnings: list[dict[str, Any]] = []
    for report in reports:
        if not report.ok:
            for v in report.violations:
                all_violations.append(v.as_dict())
        if report.warnings:
            all_warnings.append({"path": report.path, "warnings": list(report.warnings)})

    return {
        "ok": len(all_violations) == 0,
        "basis": "A185/E160",
        "checked": len(reports),
        "violations": all_violations,
        "warnings": all_warnings,
        "authority": "permission-sovereign",
        "directory_entry": "permission-directory://source-structure",
    }
