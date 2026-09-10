"""Active release verification — A181/E156.

Per A181 (certified-hot-update-persistence-and-reset-prevention), this
module provides verification functions for the active release pointer
and frontend-backend release matching.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Final

from core_system.active_release_persistence import (
    ACTIVE_POINTER_PATH,
    resolve_active_pointer,
)

_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _compute_artifact_digest(relative_path: str) -> str | None:
    """Compute SHA-256 of an artifact relative to the project root."""
    candidate = _DEFAULT_PROJECT_ROOT / relative_path
    if not candidate.is_file():
        return None
    try:
        return hashlib.sha256(candidate.read_bytes()).hexdigest()
    except OSError:
        return None


def verify_active_release(
    *,
    artifact_digests: dict[str, str] | None = None,
    pointer_path: Path = ACTIVE_POINTER_PATH,
) -> dict[str, Any]:
    """Verify the active release pointer and optional artifact digests.

    Per A181: ``VERIFICATION:post-repair/restart/reload release-id+all-roots+
    runtime-generation+frontend-backend-match+stability-window``.
    """
    pointer = resolve_active_pointer(pointer_path=pointer_path)
    if pointer is None:
        return {
            "ok": False, "pointer": None,
            "digest_mismatches": [], "reason": "no-active-release-pointer",
        }
    mismatches: list[str] = []
    if artifact_digests:
        for relative, expected in artifact_digests.items():
            actual = _compute_artifact_digest(relative)
            if actual is None or actual != expected:
                mismatches.append(relative)
    return {
        "ok": not mismatches,
        "pointer": pointer,
        "digest_mismatches": mismatches,
        "reason": "" if not mismatches else "artifact-digest-mismatch",
    }


def frontend_backend_release_match(
    frontend_release_id: str,
    backend_release_id: str,
    *,
    pointer_path: Path = ACTIVE_POINTER_PATH,
) -> dict[str, Any]:
    """Verify frontend and backend loaded the same active release (A181)."""
    pointer = resolve_active_pointer(pointer_path=pointer_path)
    active_id = pointer.release_id if pointer else ""
    match = (
        frontend_release_id == backend_release_id
        and (not active_id or frontend_release_id == active_id)
    )
    return {
        "ok": match,
        "frontend_release_id": frontend_release_id,
        "backend_release_id": backend_release_id,
        "active_release_id": active_id,
        "reason": "" if match else "frontend-backend-release-mismatch",
    }


__all__ = [
    "verify_active_release",
    "frontend_backend_release_match",
]
