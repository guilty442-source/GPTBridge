"""Source size limits and directory authority — A185/E160.

Per A185 (source-size-and-permission-managed-directory-control) and E160
(source-size-directory-authority), handwritten source modules must obey
bounded size limits, and the Permission Sovereign exclusively manages the
source-structure directory registries.

This module is a re-export facade; the implementation lives in submodules:

  * :mod:`core_system.source_size_types` — constants and dataclasses.
  * :mod:`core_system.source_size_measure` — measurement functions.
  * :mod:`core_system.source_size_verify` — verification functions.
  * :mod:`core_system.source_size_signal` — signal functions.
"""

from __future__ import annotations

from core_system.source_size_measure import (
    measure_python_source,
    measure_source,
)
from core_system.source_size_report import (
    SizeReport,
    SizeViolation,
)
from core_system.source_size_signal import size_violation_signal
from core_system.source_size_types import (
    CALLABLES_LIMIT,
    CLASS_LINE_LIMIT,
    EXCEPTION_CATEGORIES,
    FUNCTION_LINE_LIMIT,
    MODULE_LINE_LIMIT,
    PUBLIC_ENTRY_LIMIT,
    SOURCE_FILE_EXTENSIONS,
    SourceSizeMeasurement,
    WARNING_THRESHOLD,
)
from core_system.source_size_verify import (
    verify_source_directory,
    verify_source_size,
)

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
