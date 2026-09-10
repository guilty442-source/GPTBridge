"""Root containment verification functions — A201/E175, A202/E176.

Verification logic extracted from root_containment for source-size
compliance (A185/E160).  All functions are read-only path verification.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core_system.root_containment_types import (
    CANONICAL_CODE_ROOT,
    CANONICAL_ENVIRONMENT_ROOT,
    CodeRootCheck,
    EnvironmentRootCheck,
    RootContainmentReport,
)


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


# ---------------------------------------------------------------------------
# Environment root containment verification (A202/E176)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Combined root verification (A201 + A202)
# ---------------------------------------------------------------------------

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
