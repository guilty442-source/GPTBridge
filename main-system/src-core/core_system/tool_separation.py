"""Tool separation verification — A184/E159.

Per A184 (main-system-and-independent-tool-separate-individuals) and E159
(main-system-independent-tool-separation), the main-system and each
independent tool are separate runtime individuals with their own identity,
process tree, lifecycle, UI, backend, state, data, version, certificate,
health contract, resource budget, and failure boundary.

This module is a re-export facade; the implementation lives in submodules:

  * :mod:`core_system.tool_separation_types` — constants and dataclasses.
  * :mod:`core_system.tool_separation_verify` — verification functions.
  * :mod:`core_system.tool_separation_signal` — signal functions.
  * :mod:`core_system.tool_separation_aggregate` — aggregate checks.
"""

from __future__ import annotations

from core_system.tool_separation_aggregate import (
    verify_process_tree_independence,
    verify_tool_separation,
)
from core_system.tool_separation_signal import (
    reciprocal_isolation_signal,
    separation_violation_signal,
)
from core_system.tool_separation_types import (
    MAIN_SYSTEM_NON_OWNERSHIP,
    RECIPROCAL_ISOLATION_DIMENSIONS,
    SEPARATION_DIMENSIONS,
    SeparationReport,
    SeparationViolation,
)
from core_system.tool_separation_verify import (
    verify_no_shared_data_root,
    verify_reciprocal_isolation,
    verify_tool_manifest_separation,
)

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
