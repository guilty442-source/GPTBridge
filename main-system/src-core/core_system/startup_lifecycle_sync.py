"""Startup lifecycle — projection sync and window host checks (A192-E170).

Verification functions for backend-frontend projection synchronization and
window host process continuity.  Split from ``startup_lifecycle`` for
A185/E160 source-size compliance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core_system.startup_lifecycle_types import (
    HOST_REPLACEMENT_REASONS,
    SAME_HOST_OPERATIONS,
)

# ---------------------------------------------------------------------------
# A195/E169 — Backend state to frontend projection synchronization
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ProjectionSyncCheck:
    """Result of verifying backend-frontend projection sync (A195)."""

    ok: bool
    snapshot_revision: int
    cursor_revision: int
    state_hash_match: bool
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_projection_sync(
    *,
    authoritative_revision: int,
    projection_revision: int,
    cursor_revision: int,
    authoritative_hash: str,
    projection_hash: str,
) -> ProjectionSyncCheck:
    """Verify backend-frontend projection synchronization (A195/E169).

    Per A195: ``READY:projection-revision/state-hash/cursor match-
    authoritative-snapshot and subscription-current``.
    """
    violations: list[str] = []
    if projection_revision != authoritative_revision:
        violations.append(
            f"revision-mismatch:projection={projection_revision}!=auth={authoritative_revision}"
        )
    if cursor_revision != authoritative_revision:
        violations.append(
            f"cursor-mismatch:cursor={cursor_revision}!=auth={authoritative_revision}"
        )
    hash_match = projection_hash == authoritative_hash
    if not hash_match:
        violations.append("state-hash-mismatch")
    return ProjectionSyncCheck(
        ok=len(violations) == 0,
        snapshot_revision=authoritative_revision,
        cursor_revision=cursor_revision,
        state_hash_match=hash_match,
        violations=tuple(violations),
    )


# ---------------------------------------------------------------------------
# A196/E170 — Stable single window process per application session
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WindowHostCheck:
    """Result of verifying window host continuity (A196)."""

    ok: bool
    violations: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def verify_window_host_continuity(
    *,
    operation: str,
    host_changed: bool,
    replacement_reason: str = "",
) -> WindowHostCheck:
    """Verify window host process continuity (A196/E170).

    Per A196: ``INVARIANT:during-one-live-application-session exactly-one-
    window-host-process owns-all-that-application-windows``.
    """
    violations: list[str] = []

    if operation in SAME_HOST_OPERATIONS and host_changed:
        if replacement_reason and replacement_reason in HOST_REPLACEMENT_REASONS:
            pass  # Valid replacement reason overrides same-host requirement
        else:
            violations.append(
                f"host-changed-on-{operation}:must-reuse-same-host"
            )

    if host_changed and replacement_reason:
        if replacement_reason not in HOST_REPLACEMENT_REASONS:
            violations.append(
                f"invalid-replacement-reason:{replacement_reason}"
            )

    return WindowHostCheck(
        ok=len(violations) == 0,
        violations=tuple(violations),
    )
