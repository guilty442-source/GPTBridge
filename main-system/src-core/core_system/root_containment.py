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
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Final

# ---------------------------------------------------------------------------
# Canonical roots (A201: CANONICAL-CODE-ROOT, A202: CANONICAL-ENVIRONMENT-ROOT)
# ---------------------------------------------------------------------------

CANONICAL_CODE_ROOT: Final[str] = r"E:\GPTBridge"
CANONICAL_ENVIRONMENT_ROOT: Final[str] = r"E:\AI"

# A202: REQUIRED-LAYOUT under E:\AI
ENVIRONMENT_REQUIRED_LAYOUT: Final[tuple[str, ...]] = (
    "runtimes",
    "environments",
    "packages",
    "toolchains",
    "models",
    "services",
    "caches",
)

# A202: EXCEPTION-1 — Windows 11 native tools at OS-owned paths
WINDOWS11_NATIVE_EXCEPTION: Final[str] = "windows-11-native-tools"

# A202: EXCEPTION-2 — Ollama at exact registered C-drive paths
OLLAMA_C_DRIVE_EXCEPTION: Final[str] = "ollama-c-drive"

# A201: FORBIDDEN — outside-root locations
FORBIDDEN_CODE_LOCATIONS: Final[tuple[str, ...]] = (
    "user-profile",
    "Desktop",
    "Documents",
    "Downloads",
    "AppData",
    "ProgramData",
    "Windows",
    "System",
    "other-drive",
    "UNC",
    "network",
    "share",
    "cloud-sync",
    "temp",
)

# A201: CHECKPOINTS — when to verify code root
CODE_ROOT_CHECKPOINTS: Final[tuple[str, ...]] = (
    "startup",
    "code-change",
    "tool-call",
    "Git-operation",
    "dependency-install",
    "generation",
    "build",
    "test",
    "package",
    "hot-update",
    "repair",
    "runtime-load",
)


# ---------------------------------------------------------------------------
# Path resolution helpers (A201: LOCATION-RULE)
# ---------------------------------------------------------------------------

def _normalize_path(path: str | Path) -> Path:
    """Normalize a path for root containment checking.

    Per A201: ``after absolute-normalization+case-folding+drive-resolution+
    environment-expansion+short-name-resolution+symlink/junction/mount/
    reparse-target-resolution``.
    """
    # Expand environment variables
    expanded = os.path.expandvars(str(path))
    # Resolve to absolute, normalizing separators
    p = Path(expanded).resolve(strict=False)
    return p


def _is_descendant_of(path: Path, root: Path) -> bool:
    """Check if path is root or a strict descendant using component boundary.

    Per A201: ``ROOT-BOUNDARY:path-component boundary not-string-prefix``.
    """
    try:
        resolved_path = path.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except (OSError, ValueError):
        return False

    # Compare component-by-component (not string prefix)
    path_parts = resolved_path.parts
    root_parts = resolved_root.parts

    if len(path_parts) < len(root_parts):
        return False

    # Case-insensitive comparison on Windows
    for i, root_part in enumerate(root_parts):
        if i >= len(path_parts):
            return False
        if str(path_parts[i]).lower() != str(root_part).lower():
            return False

    return True


# ---------------------------------------------------------------------------
# Code root containment verification (A201/E175)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CodeRootCheck:
    """Result of verifying a path is within the canonical code root (A201)."""

    ok: bool
    requested_path: str
    resolved_path: str
    root: str
    is_descendant: bool
    violation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_code_root_containment(
    path: str | Path,
    *,
    root: str = CANONICAL_CODE_ROOT,
) -> CodeRootCheck:
    """Verify a path is within the canonical code root (A201/E175).

    Per A201: ``every-in-scope path after absolute-normalization+case-folding+
    drive-resolution+environment-expansion+short-name-resolution+symlink/
    junction/mount/reparse-target-resolution must-equal-root or be-strict-
    descendant-of-root``.
    """
    root_path = Path(root).resolve(strict=False)
    resolved = _normalize_path(path)
    is_desc = _is_descendant_of(resolved, root_path)

    violation = ""
    if not is_desc:
        # Check for specific violation types
        resolved_str = str(resolved)
        if resolved_str.startswith(r"\\"):
            violation = "network-path"
        elif not resolved.exists() and ".." in str(path):
            violation = "parent-traversal"
        else:
            violation = "outside-root"

    return CodeRootCheck(
        ok=is_desc,
        requested_path=str(path),
        resolved_path=str(resolved),
        root=str(root_path),
        is_descendant=is_desc,
        violation=violation,
    )


def code_root_violation_signal(
    check: CodeRootCheck,
    *,
    operation: str = "",
    actor: str = "",
) -> dict[str, Any]:
    """Produce a fail-closed signal for a code root violation (A201: FAILURE).

    Per A201: ``FAILURE:any-unresolved/ambiguous/nonexistent/parent/reparse-
    loop/case-alias/device-path/network-path/outside-root=>fail-closed+no-
    create/write/copy/move/load/import/execute+report-to-user``.
    """
    return {
        "signal_type": "code-root-violation",
        "authority": "signal-only",
        "basis": "A201/E175",
        "ok": check.ok,
        "requested_path": check.requested_path,
        "resolved_path": check.resolved_path,
        "root": check.root,
        "violation": check.violation,
        "operation": operation,
        "actor": actor,
        "action": "fail-closed+no-create/write/copy/move/load/import/execute+report-to-user",
        "quarantine": True,
    }


# ---------------------------------------------------------------------------
# Environment root containment verification (A202/E176)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EnvironmentRootCheck:
    """Result of verifying a path is within the canonical environment root (A202)."""

    ok: bool
    requested_path: str
    resolved_path: str
    root: str
    is_descendant: bool
    is_windows11_native_exception: bool
    is_ollama_c_drive_exception: bool
    violation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_environment_root_containment(
    path: str | Path,
    *,
    root: str = CANONICAL_ENVIRONMENT_ROOT,
    is_windows11_native: bool = False,
    is_ollama_registered: bool = False,
) -> EnvironmentRootCheck:
    """Verify a path is within the canonical environment root (A202/E176).

    Per A202: ``each-installation/content/store/cache after-full-canonical/
    reparse resolution must-be-E:\\AI or-descendant unless-one-declared-
    exception``.

    Exceptions:
      * Windows 11 native tools at OS-owned paths (Exception 1).
      * Ollama runtime/download/model-store at exact registered C-drive
        paths (Exception 2).
    """
    root_path = Path(root).resolve(strict=False)
    resolved = _normalize_path(path)
    is_desc = _is_descendant_of(resolved, root_path)

    # Check exceptions
    win11_exc = is_windows11_native
    ollama_exc = is_ollama_registered

    ok = is_desc or win11_exc or ollama_exc

    violation = ""
    if not ok:
        resolved_str = str(resolved)
        if resolved_str.startswith(r"\\"):
            violation = "network-path"
        elif "Program Files" in resolved_str or "ProgramData" in resolved_str:
            violation = "system-global-install"
        elif "AppData" in resolved_str:
            violation = "user-profile-install"
        else:
            violation = "outside-environment-root"

    return EnvironmentRootCheck(
        ok=ok,
        requested_path=str(path),
        resolved_path=str(resolved),
        root=str(root_path),
        is_descendant=is_desc,
        is_windows11_native_exception=win11_exc,
        is_ollama_c_drive_exception=ollama_exc,
        violation=violation,
    )


def environment_root_violation_signal(
    check: EnvironmentRootCheck,
    *,
    dependency_id: str = "",
) -> dict[str, Any]:
    """Produce a fail-closed signal for an environment root violation (A202: FAILURE).

    Per A202: ``FAILURE:outside/missing/ambiguous/unverified dependency-or-
    exception=>deny-load/execute+affected-capability-unavailable+typed-report``.
    """
    return {
        "signal_type": "environment-root-violation",
        "authority": "signal-only",
        "basis": "A202/E176",
        "ok": check.ok,
        "requested_path": check.requested_path,
        "resolved_path": check.resolved_path,
        "root": check.root,
        "violation": check.violation,
        "dependency_id": dependency_id,
        "action": "deny-load/execute+affected-capability-unavailable+typed-report",
        "windows11_exception": check.is_windows11_native_exception,
        "ollama_exception": check.is_ollama_c_drive_exception,
    }


# ---------------------------------------------------------------------------
# Combined root verification (A201 + A202)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RootContainmentReport:
    """Combined code-root and environment-root containment report."""

    code_root_ok: bool
    environment_root_ok: bool
    code_check: CodeRootCheck | None
    environment_check: EnvironmentRootCheck | None

    @property
    def ok(self) -> bool:
        return self.code_root_ok and self.environment_root_ok

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code_root_ok": self.code_root_ok,
            "environment_root_ok": self.environment_root_ok,
            "code_check": self.code_check.as_dict() if self.code_check else None,
            "environment_check": self.environment_check.as_dict() if self.environment_check else None,
            "basis": "A201/E175+A202/E176",
        }


def verify_root_containment(
    code_path: str | Path | None = None,
    env_path: str | Path | None = None,
    *,
    is_windows11_native: bool = False,
    is_ollama_registered: bool = False,
) -> RootContainmentReport:
    """Verify both code-root and environment-root containment (A201+A202)."""
    code_check = None
    env_check = None
    code_ok = True
    env_ok = True

    if code_path is not None:
        code_check = verify_code_root_containment(code_path)
        code_ok = code_check.ok

    if env_path is not None:
        env_check = verify_environment_root_containment(
            env_path,
            is_windows11_native=is_windows11_native,
            is_ollama_registered=is_ollama_registered,
        )
        env_ok = env_check.ok

    return RootContainmentReport(
        code_root_ok=code_ok,
        environment_root_ok=env_ok,
        code_check=code_check,
        environment_check=env_check,
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
