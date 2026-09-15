"""Organize plan preview, execution, and automation for file sorter CLI.

This module re-exports names that moved into sibling modules so existing
imports (``from .cli_organize import ...``) continue to work unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ..infrastructure.sorter_engine import (
    ProfileSnapshot,
    SorterV2Error,
    list_profiles,
    prune_state,
    recover_transactions,
    save_profile,
)
from .cli_models import FileSorterError
from .cli_organize_automation import (
    run_enabled_profiles_once,
)
from .cli_organize_plan import (
    _effective_state_root,
    _facade_override,
    apply_organize_plan,
    organize_files,
    preview_organize_files,
)
from .cli_paths import resolve_target_dir
from .cli_rules import (
    _load_profile_snapshot,
    _uses_explicit_legacy_rules_path,
)


def _disable_other_scan_targets(
    active_profile_id: str,
    *,
    state_root: str | Path | None,
) -> None:
    """Keep exactly one active scan target: the folder the user selected.

    Selecting a folder makes it the scanned folder; every other profile stops
    its background classification so a stale selection can never keep scanning.
    """

    for other in list_profiles(state_root=state_root):
        if other.profile_id == active_profile_id or not other.enabled:
            continue
        try:
            save_profile(other, enabled=False)
        except SorterV2Error:
            continue



def select_scan_target(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> ProfileSnapshot:
    """Follow the user's folder selection as the scanned folder.

    When background classification is already active for some folder, moving
    the selection to another folder switches the single active scan target to
    that folder.  Nothing is enabled while every profile is off: enabling
    automatic moves stays an explicit user decision.
    """

    if _uses_explicit_legacy_rules_path():
        raise FileSorterError("Profiles require the V2 user state repository.")
    snapshot = _load_profile_snapshot(
        target_dir,
        state_root=state_root,
        profile=profile,
    )
    if snapshot.enabled:
        return snapshot
    others_enabled = any(
        other.enabled
        for other in list_profiles(state_root=state_root)
        if other.profile_id != snapshot.profile_id
    )
    if not others_enabled:
        return snapshot
    try:
        saved = save_profile(
            snapshot,
            enabled=True,
            acknowledge_migration_review=snapshot.migration_required_review,
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error
    _disable_other_scan_targets(saved.profile_id, state_root=state_root)
    return saved



def configure_profile_enabled(
    target_dir: str | Path,
    enabled: bool,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> ProfileSnapshot:
    if _uses_explicit_legacy_rules_path():
        raise FileSorterError("Profiles require the V2 user state repository.")
    snapshot = _load_profile_snapshot(
        target_dir,
        state_root=state_root,
        profile=profile,
    )
    try:
        saved = save_profile(
            snapshot,
            enabled=enabled,
            acknowledge_migration_review=(
                enabled and snapshot.migration_required_review
            ),
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error
    if enabled:
        _disable_other_scan_targets(saved.profile_id, state_root=state_root)
    return saved



def configure_duplicate_trash_enabled(
    target_dir: str | Path,
    enabled: bool,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> ProfileSnapshot:
    """Explicitly opt a profile into or out of recoverable duplicate cleanup."""

    if _uses_explicit_legacy_rules_path():
        raise FileSorterError("Duplicate recycling requires the V2 user state repository.")
    snapshot = _load_profile_snapshot(
        target_dir,
        state_root=state_root,
        profile=profile,
    )
    try:
        return save_profile(
            snapshot,
            duplicate_trash_enabled=enabled,
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error



def scan_after_keyword_addition(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Immediately observe a target after at least one new rule is saved."""

    target = str(Path(target_dir).resolve())
    runner = _facade_override(
        "run_enabled_profiles_once",
        run_enabled_profiles_once,
    )
    for report in runner(state_root=state_root):
        if str(report.get("target_dir", "")) == target:
            return report
    return None



def enabled_profile_targets(
    *,
    state_root: str | Path | None = None,
) -> list[str]:
    """Return validated roots for enabled profiles used by realtime monitoring."""

    effective_root = _effective_state_root(state_root)
    targets: set[str] = set()
    for snapshot in list_profiles(state_root=effective_root):
        if (
            (not snapshot.enabled and not snapshot.duplicate_trash_enabled)
            or snapshot.migration_required_review
        ):
            continue
        try:
            target = resolve_target_dir(snapshot.target_dir)
        except FileSorterError:
            continue
        targets.add(str(target))
    return sorted(targets, key=os.path.normcase)
