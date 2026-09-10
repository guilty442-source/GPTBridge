"""Source size measurement functions — A185/E160.

Effective line counting and source file measurement against A185 default
limits.
"""

from __future__ import annotations

import ast

from pathlib import Path

from core_system.source_size_types import (
    CALLABLES_LIMIT,
    CLASS_LINE_LIMIT,
    FUNCTION_LINE_LIMIT,
    MODULE_LINE_LIMIT,
    PUBLIC_ENTRY_LIMIT,
    SOURCE_FILE_EXTENSIONS,
    SourceSizeMeasurement,
)


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
        largest_class_name=largest_cls_name,
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
        largest_class_name="(not-measured)",
        public_entrypoints=0,
        authored_callables=0,
        module_limit=module_limit,
        function_limit=function_limit,
        class_limit=class_limit,
        entry_limit=entry_limit,
        callables_limit=callables_limit,
    )
