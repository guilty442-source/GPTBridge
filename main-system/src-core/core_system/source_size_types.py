"""Source size types and constants — A185/E160.

Default limits, exception categories, and the SourceSizeMeasurement
dataclass.  This module has no imports from report, measurement,
verification, or signal submodules.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
    largest_class_name: str
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
