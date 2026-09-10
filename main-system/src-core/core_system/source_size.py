"""Source size limits and directory authority — A185/E160.

Per A185 (source-size-and-permission-managed-directory-control) and E160
(source-size-directory-authority), handwritten source modules must obey
bounded size limits, and the Permission Sovereign exclusively manages the
source-structure directory registries.

Default limits (A185: DEFAULT-LIMITS):

  * Module: <= 500 effective lines
  * Function/method: <= 50 effective lines
  * Class: <= 300 effective lines
  * Public entrypoints per module: <= 3
  * Authored functions or classes per module: <= 12

Effective line (A185: EFFECTIVE-LINE): nonblank + noncomment physical line
including decorator + signature + control-flow + embedded template/JSX/SQL.

Warning threshold (A185: WARNING): 80% of any limit.

Block (A185: BLOCK): exceed-limit-before-merge + release + startup-activation-
of-changed-module.

Directory authority (A185: DIRECTORY-OWNER): Permission Sovereign exclusively
manages operational source-structure directories at
``permission-directory://source-structure``.

Exception categories (A185: EXCEPTION-CATEGORIES): auto-generated, pure-data-
table, registry, language-required-generated-interface, single-atomic-
migration/schema.  Each exception is file-specific, typed, proved, least-scope,
reviewed, and expiring-or-justified-permanent.

This module provides **read-only measurement and verification**.  It never
mutates source files, directories, or registries; it only measures and
reports violations as signals for the sovereign decision chain.
"""

from __future__ import annotations

import ast
import tokenize
import io
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# Default limits (A185: DEFAULT-LIMITS)
# ---------------------------------------------------------------------------

MODULE_LINE_LIMIT: Final[int] = 500
FUNCTION_LINE_LIMIT: Final[int] = 50
CLASS_LINE_LIMIT: Final[int] = 300
PUBLIC_ENTRY_LIMIT: Final[int] = 3
CALLABLES_LIMIT: Final[int] = 12

# A185: WARNING — 80% of any limit
WARNING_THRESHOLD: Final[float] = 0.80

# A185: FILE-TYPES — source file extensions subject to size limits
SOURCE_FILE_EXTENSIONS: Final[frozenset[str]] = frozenset({
    ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx",
    ".c", ".h", ".cpp", ".hpp", ".inl", ".cs", ".sql",
})

# A185: EXCEPTION-CATEGORIES
EXCEPTION_CATEGORIES: Final[tuple[str, ...]] = (
    "auto-generated",
    "pure-data-table",
    "registry",
    "language-required-generated-interface",
    "single-atomic-migration-schema",
)


# ---------------------------------------------------------------------------
# Measurement result
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceSizeMeasurement:
    """Measurement of a single source file's effective line counts."""

    path: str
    file_type: str
    module_effective_lines: int
    largest_function_lines: int
    largest_function_name: str
    largest_class_lines: int
    largest_class_name: int
    public_entrypoints: int
    authored_callables: int
    module_limit: int
    function_limit: int
    class_limit: int
    entry_limit: int
    callables_limit: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def module_exceeds(self) -> bool:
        return self.module_effective_lines > self.module_limit

    @property
    def function_exceeds(self) -> bool:
        return self.largest_function_lines > self.function_limit

    @property
    def class_exceeds(self) -> bool:
        return self.largest_class_lines > self.class_limit

    @property
    def entry_exceeds(self) -> bool:
        return self.public_entrypoints > self.entry_limit

    @property
    def callables_exceed(self) -> bool:
        return self.authored_callables > self.callables_limit

    @property
    def ok(self) -> bool:
        return not (
            self.module_exceeds
            or self.function_exceeds
            or self.class_exceeds
            or self.entry_exceeds
            or self.callables_exceed
        )

    @property
    def warnings(self) -> list[str]:
        """Return warning messages for dimensions at or above 80% of limit."""
        result: list[str] = []
        if self.module_effective_lines >= self.module_limit * WARNING_THRESHOLD:
            result.append(
                f"module at {self.module_effective_lines}/{self.module_limit} "
                f"({self.module_effective_lines / self.module_limit:.0%})"
            )
        if self.largest_function_lines >= self.function_limit * WARNING_THRESHOLD:
            result.append(
                f"function {self.largest_function_name} at "
                f"{self.largest_function_lines}/{self.function_limit}"
            )
        if self.largest_class_lines >= self.class_limit * WARNING_THRESHOLD:
            result.append(
                f"class {self.largest_class_name} at "
                f"{self.largest_class_lines}/{self.class_limit}"
            )
        return result


@dataclass(frozen=True)
class SizeViolation:
    """A typed source size violation signal (A185/E160)."""

    dimension: str
    path: str
    measured: int
    limit: int
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SizeReport:
    """Result of verifying source size limits for a file."""

    ok: bool
    path: str
    measurement: SourceSizeMeasurement | None
    violations: tuple[SizeViolation, ...] = ()
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "measurement": self.measurement.as_dict() if self.measurement else None,
            "violations": [v.as_dict() for v in self.violations],
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# Effective line counting (A185: EFFECTIVE-LINE)
# ---------------------------------------------------------------------------

def _count_effective_lines_python(source: str) -> tuple[int, list[tuple[str, int, int]]]:
    """Count effective lines in Python source and measure callables.

    Returns (total_effective_lines, [(name, start_line, end_line), ...]).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0, []

    lines = source.splitlines()
    total = 0
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            total += 1

    callables: list[tuple[str, int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
            effective = _effective_line_range(lines, node.lineno, end_line)
            callables.append((node.name, node.lineno, effective))
        elif isinstance(node, ast.ClassDef):
            end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
            effective = _effective_line_range(lines, node.lineno, end_line)
            callables.append((node.name, node.lineno, effective))

    return total, callables


def _effective_line_range(lines: list[str], start: int, end: int) -> int:
    """Count effective lines in a line range (1-based, inclusive)."""
    count = 0
    for index in range(start - 1, min(end, len(lines))):
        stripped = lines[index].strip()
        if stripped and not stripped.startswith("#"):
            count += 1
    return count


def _count_public_entrypoints_python(source: str) -> int:
    """Count public entrypoints (non-underscore-prefixed top-level functions)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0
    count = 0
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("_"):
                count += 1
    return count


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def measure_python_source(
    path: Path,
    *,
    module_limit: int = MODULE_LINE_LIMIT,
    function_limit: int = FUNCTION_LINE_LIMIT,
    class_limit: int = CLASS_LINE_LIMIT,
    entry_limit: int = PUBLIC_ENTRY_LIMIT,
    callables_limit: int = CALLABLES_LIMIT,
) -> SourceSizeMeasurement:
    """Measure a Python source file against A185 default limits."""
    source = path.read_text(encoding="utf-8")
    total_lines, callables = _count_effective_lines_python(source)

    largest_func = 0
    largest_func_name = ""
    largest_cls = 0
    largest_cls_name = ""
    authored = 0

    for name, _start, effective in callables:
        authored += 1
        # Heuristic: classes are typically PascalCase; functions are snake_case.
        # We use AST node type, but here we only have names.  We count the
        # largest callable overall and track function vs class separately
        # by checking if the name looks like a class (PascalCase).
        if name and name[0].isupper() and "_" not in name:
            if effective > largest_cls:
                largest_cls = effective
                largest_cls_name = name
        else:
            if effective > largest_func:
                largest_func = effective
                largest_func_name = name

    public_entries = _count_public_entrypoints_python(source)

    return SourceSizeMeasurement(
        path=str(path),
        file_type=".py",
        module_effective_lines=total_lines,
        largest_function_lines=largest_func,
        largest_function_name=largest_func_name,
        largest_class_lines=largest_cls,
        largest_class_name=largest_cls,
        public_entrypoints=public_entries,
        authored_callables=authored,
        module_limit=module_limit,
        function_limit=function_limit,
        class_limit=class_limit,
        entry_limit=entry_limit,
        callables_limit=callables_limit,
    )


def measure_source(
    path: Path,
    *,
    module_limit: int = MODULE_LINE_LIMIT,
    function_limit: int = FUNCTION_LINE_LIMIT,
    class_limit: int = CLASS_LINE_LIMIT,
    entry_limit: int = PUBLIC_ENTRY_LIMIT,
    callables_limit: int = CALLABLES_LIMIT,
) -> SourceSizeMeasurement | None:
    """Measure any supported source file.  Returns None for unsupported types."""
    ext = path.suffix.lower()
    if ext not in SOURCE_FILE_EXTENSIONS:
        return None
    if ext in (".py", ".pyi"):
        return measure_python_source(
            path,
            module_limit=module_limit,
            function_limit=function_limit,
            class_limit=class_limit,
            entry_limit=entry_limit,
            callables_limit=callables_limit,
        )
    # For non-Python files, measure effective lines only (no AST).
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    effective = sum(
        1 for line in lines
        if line.strip() and not line.strip().startswith(("//", "/*", "*", "--", "#"))
    )
    return SourceSizeMeasurement(
        path=str(path),
        file_type=ext,
        module_effective_lines=effective,
        largest_function_lines=0,
        largest_function_name="(not-measured)",
        largest_class_lines=0,
        largest_class_name=0,
        public_entrypoints=0,
        authored_callables=0,
        module_limit=module_limit,
        function_limit=function_limit,
        class_limit=class_limit,
        entry_limit=entry_limit,
        callables_limit=callables_limit,
    )


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_source_size(
    path: Path,
    *,
    module_limit: int = MODULE_LINE_LIMIT,
    function_limit: int = FUNCTION_LINE_LIMIT,
    class_limit: int = CLASS_LINE_LIMIT,
    entry_limit: int = PUBLIC_ENTRY_LIMIT,
    callables_limit: int = CALLABLES_LIMIT,
) -> SizeReport:
    """Verify a source file against A185/E160 size limits.

    Returns a SizeReport with ok, measurement, violations, and warnings.
    """
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
            ok=True,
            path=str(path),
            measurement=None,
            violations=(),
            warnings=(),
        )

    violations: list[SizeViolation] = []
    if measurement.module_exceeds:
        violations.append(SizeViolation(
            dimension="module",
            path=str(path),
            measured=measurement.module_effective_lines,
            limit=measurement.module_limit,
            detail=f"module exceeds {measurement.module_limit} effective lines",
        ))
    if measurement.function_exceeds:
        violations.append(SizeViolation(
            dimension="function",
            path=str(path),
            measured=measurement.largest_function_lines,
            limit=measurement.function_limit,
            detail=f"function {measurement.largest_function_name} exceeds {measurement.function_limit} lines",
        ))
    if measurement.class_exceeds:
        violations.append(SizeViolation(
            dimension="class",
            path=str(path),
            measured=measurement.largest_class_lines,
            limit=measurement.class_limit,
            detail=f"class exceeds {measurement.class_limit} lines",
        ))
    if measurement.entry_exceeds:
        violations.append(SizeViolation(
            dimension="public-entrypoints",
            path=str(path),
            measured=measurement.public_entrypoints,
            limit=measurement.entry_limit,
            detail=f"public entrypoints exceed {measurement.entry_limit}",
        ))
    if measurement.callables_exceed:
        violations.append(SizeViolation(
            dimension="callables",
            path=str(path),
            measured=measurement.authored_callables,
            limit=measurement.callables_limit,
            detail=f"authored callables exceed {measurement.callables_limit}",
        ))

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


def size_violation_signal(
    report: SizeReport,
) -> dict[str, Any]:
    """Produce an information-layer signal for a size violation (A185/E160).

    Per A185: ``BLOCK:exceed-limit-before-merge+release+startup-activation-of-
    changed-module``.  This function produces the signal payload that must be
    routed through the information layer; it never performs file mutation,
    split, or directory update.
    """
    return {
        "signal_type": "source-size-violation",
        "authority": "signal-only",
        "basis": "A185/E160",
        "path": report.path,
        "ok": report.ok,
        "violations": [v.as_dict() for v in report.violations],
        "warnings": list(report.warnings),
        "action_required": "block-merge+block-release+block-startup-activation",
        "priority": "split-over-exception",
        "directory_entry": "permission-directory://source-structure",
    }


__all__ = [
    "CALLABLES_LIMIT",
    "CLASS_LINE_LIMIT",
    "EXCEPTION_CATEGORIES",
    "FUNCTION_LINE_LIMIT",
    "MODULE_LINE_LIMIT",
    "PUBLIC_ENTRY_LIMIT",
    "SizeReport",
    "SizeViolation",
    "SourceSizeMeasurement",
    "SOURCE_FILE_EXTENSIONS",
    "WARNING_THRESHOLD",
    "measure_python_source",
    "measure_source",
    "size_violation_signal",
    "verify_source_directory",
    "verify_source_size",
]
