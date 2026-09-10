"""Absolute code root and environment root containment — A201/E175 and A202/E176.

Per A201 (absolute-gptbridge-code-root-containment), A202 (absolute-
environment-dependency-root), E175 (absolute-code-root), and E176 (absolute-
environment-root), all GPTBridge project code must reside within the
canonical code root ``E:\\GPTBridge``, and all managed environment
dependencies must reside within the canonical environment root ``E:\\AI``
(with two declared exceptions).

Key invariants (A201/E175):

  * **Canonical code root** — ``E:\\GPTBridge``.
  * **Scope** — all GPTBridge-owned/authored/copied/generated/transformed/
    downloaded/vendored/patched/extracted/staged/executable project source,
    scripts, modules, packages, plugins, extensions, hooks, launchers,
    workers, migrations, code-templates, embedded-code, build-inputs,
    runtime-loaded-code, hot-update/hot-reload artifacts, test-code,
    repair-code, AI/programming-tool output, Git-worktrees, object-database,
    index, hooks.
  * **Location rule** — every in-scope path after absolute normalization +
    case-folding + drive-resolution + environment-expansion + short-name-
    resolution + symlink/junction/mount/reparse-target-resolution must equal
    root or be strict descendant of root.
  * **Root boundary** — path-component boundary, not string-prefix.
  * **Git** — main repository + .git + all worktrees + shared-object-database
    + hooks must reside within root; external-worktree-path forbidden.
  * **Failure** — any unresolved/ambiguous/nonexistent/parent/reparse-loop/
    case-alias/device-path/network-path/outside-root => fail-closed.

Key invariants (A202/E176):

  * **Canonical environment root** — ``E:\\AI``.
  * **Scope** — all GPTBridge-managed Python/Node/.NET/native runtimes,
    virtual-environments, interpreters, SDKs, compilers, linkers, package-
    managers, dependency-packages, native-libraries, drivers-not-OS-managed,
    CLI-tools, all-non-Ollama-model-runtimes/models, PostgreSQL/Qdrant
    managed-binaries, build-toolchains, dependency-download-cache.
  * **Exception 1** — Windows 11 native tools at OS-owned paths.
  * **Exception 2** — Ollama runtime + download + managed-model-store at
    exact registered C-drive paths.
  * **GPTBridge project code** — remains only under ``E:\\GPTBridge`` per
    A201; must not be installed/copied/generated into ``E:\\AI`` or C-drive.

This module provides **read-only path verification**.  It never creates,
writes, copies, moves, loads, imports, or executes files.

This file is a re-export facade; implementation lives in:
  * ``root_containment_types``   — constants and dataclasses
  * ``root_containment_verify``  — verification functions
  * ``root_containment_signal``  — signal functions
"""

from __future__ import annotations

from core_system.root_containment_types import (
    CANONICAL_CODE_ROOT,
    CANONICAL_ENVIRONMENT_ROOT,
    CODE_ROOT_CHECKPOINTS,
    CodeRootCheck,
    EnvironmentRootCheck,
    ENVIRONMENT_REQUIRED_LAYOUT,
    FORBIDDEN_CODE_LOCATIONS,
    OLLAMA_C_DRIVE_EXCEPTION,
    RootContainmentReport,
    WINDOWS11_NATIVE_EXCEPTION,
)
from core_system.root_containment_verify import (
    verify_code_root_containment,
    verify_environment_root_containment,
    verify_root_containment,
)
from core_system.root_containment_signal import (
    code_root_violation_signal,
    environment_root_violation_signal,
)

__all__ = [
    "CANONICAL_CODE_ROOT",
    "CANONICAL_ENVIRONMENT_ROOT",
    "CODE_ROOT_CHECKPOINTS",
    "CodeRootCheck",
    "EnvironmentRootCheck",
    "ENVIRONMENT_REQUIRED_LAYOUT",
    "FORBIDDEN_CODE_LOCATIONS",
    "OLLAMA_C_DRIVE_EXCEPTION",
    "RootContainmentReport",
    "WINDOWS11_NATIVE_EXCEPTION",
    "code_root_violation_signal",
    "environment_root_violation_signal",
    "verify_code_root_containment",
    "verify_environment_root_containment",
    "verify_root_containment",
]
