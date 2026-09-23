"""Restore OS read-only attributes on protected governance sources.

Git checkouts (clone, merge --ff-only, reset) write working-tree files
without the Windows FILE_ATTRIBUTE_READONLY bit.  The governance audit's
``protected-source-readonly`` check requires that bit, so every
fast-forwarded worktree failed its next self-commit audit until a human
re-applied the attributes by hand.

This module re-applies the declared invariant at the two points where the
attribute is mechanically lost or required: after a governed fast-forward
in ``workspace_sync`` and before the commit audit in ``self_commit``.  It
only ever sets the file attribute — content is never touched, and the
audit remains the authority on the invariant (a failed restore still
surfaces there).
"""

from __future__ import annotations

import logging
import stat
from pathlib import Path

_logger = logging.getLogger(__name__)


def protected_source_paths() -> tuple[str, ...]:
    """Live protected-source list, same derivation as the audit check."""
    from governance_rule.governance_policy import governance_policy_snapshot
    from governance_rule.permission_directory.directory_authority import (
        directory_authority_snapshot,
    )

    policy = governance_policy_snapshot()
    directory = directory_authority_snapshot()
    shared_root = policy.shared_layer.source_root
    return (
        *policy.authority_files,
        *directory.managed_read_only_registry_paths,
        # The audit manifest also requires read-only on the shared-layer
        # entry modules (``shared-layer-source-readonly:*``); they are not
        # part of the protected-source registry, so restore them here the
        # same way the manifest derives them.
        *(f"{shared_root}/shared_layer/{name}" for name in (
            "__init__.py", "channel.py", "store.py",
        )),
    )


def _is_read_only(path: Path) -> bool:
    attributes = int(getattr(path.stat(), "st_file_attributes", 0) or 0)
    flag = int(getattr(stat, "FILE_ATTRIBUTE_READONLY", 0) or 0)
    return bool(flag and attributes & flag)


def restore_protected_readonly(root: str | Path) -> dict[str, object]:
    """Re-apply read-only on protected sources under ``root``.

    Returns ``{"checked": n, "restored": [...], "missing": [...]}``.
    Individual failures are logged and skipped — never raised — so the
    caller's audit step stays the single authority on the invariant.
    """
    from ..codex_update_pipeline import _set_read_only

    root_path = Path(root)
    restored: list[str] = []
    missing: list[str] = []
    checked = 0
    for relative in protected_source_paths():
        target = root_path / relative
        if not target.is_file():
            missing.append(relative)
            continue
        checked += 1
        if _is_read_only(target):
            continue
        try:
            _set_read_only(target, True)
        except OSError as error:
            _logger.warning(
                "protected source attribute restore failed: %s: %s",
                relative,
                error,
            )
            continue
        if _is_read_only(target):
            restored.append(relative)
    return {"checked": checked, "restored": restored, "missing": missing}
