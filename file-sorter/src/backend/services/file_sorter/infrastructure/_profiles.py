"""Profile load, save, and list operations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from ._constants import (
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    DEFAULT_QUIET_SECONDS,
    SCHEMA_VERSION,
    RuleConflictError,
    SorterV2Error,
)
from ._io_utils import _atomic_write_json
from ._locking import _ExclusiveFileLock
from ._models import ProfileSnapshot, _utc_now
from ._paths import (
    _clean_patterns,
    _clean_rule_dicts,
    _same_path_identity,
    _validated_state_document_path,
    _validated_target_directory,
    profile_id_for,
    profile_path,
)


def _read_profile_document(
    path: Path,
    *,
    state_root: str | Path | None,
) -> ProfileSnapshot:
    path = _validated_state_document_path(
        path,
        state_root=state_root,
        category="profiles",
        relative_parts=2,
        require_exists=True,
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SorterV2Error(f"Cannot read profile {path}: {error}") from error
    if not isinstance(value, dict):
        raise SorterV2Error(f"Invalid profile document: {path}")
    target_dir = str(value.get("target_dir") or value.get("target") or "").strip()
    profile_id = str(value.get("profile_id", path.parent.name)).strip()
    profile_name = (
        str(value["profile_name"])
        if value.get("profile_name") is not None
        else None
    )
    raw_rules = value.get("rules", [])
    if not target_dir or not isinstance(raw_rules, list):
        raise SorterV2Error(f"Invalid profile document: {path}")
    raw_target = Path(target_dir).expanduser()
    if not raw_target.is_absolute():
        raise SorterV2Error(f"Profile target is not absolute: {path}")
    canonical_target = _validated_target_directory(
        raw_target,
        label=f"Profile target in {path}",
    )
    expected_profile_id = profile_id_for(canonical_target, profile_name)
    if (
        path.name != "profile.json"
        or path.parent.name != profile_id
        or profile_id != expected_profile_id
    ):
        raise SorterV2Error(f"Profile identity does not match its state path: {path}")
    return ProfileSnapshot(
        profile_id=profile_id,
        profile_name=profile_name,
        target_dir=str(canonical_target),
        revision=max(0, int(value.get("revision", 0))),
        enabled=bool(value.get("enabled", True)),
        duplicate_trash_enabled=bool(
            value.get("duplicate_trash_enabled", False)
        ),
        quiet_seconds=max(0.0, float(value.get("quiet_seconds", DEFAULT_QUIET_SECONDS))),
        include=_clean_patterns(value.get("include"), default=DEFAULT_INCLUDE),
        exclude=_clean_patterns(value.get("exclude"), default=DEFAULT_EXCLUDE),
        rules=_clean_rule_dicts(raw_rules),
        path=path,
        migrated_from=(
            str(value["migrated_from"])
            if value.get("migrated_from")
            else None
        ),
        migration_required_review=bool(
            value.get("migration_required_review", False)
        ),
        migration_rejected_rule_count=max(
            0,
            int(value.get("migration_rejected_rule_count", 0)),
        ),
    )


def _profile_document(snapshot: ProfileSnapshot) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": snapshot.profile_id,
        "profile_name": snapshot.profile_name,
        "target": snapshot.target_dir,
        "target_dir": snapshot.target_dir,
        "revision": snapshot.revision,
        "enabled": snapshot.enabled,
        "duplicate_trash_enabled": snapshot.duplicate_trash_enabled,
        "quiet_seconds": snapshot.quiet_seconds,
        "include": list(snapshot.include),
        "exclude": list(snapshot.exclude),
        "rules_authority": "tool-settings:file-sorter/keyword_rules.json",
        "migrated_from": snapshot.migrated_from,
        "migration_required_review": snapshot.migration_required_review,
        "migration_rejected_rule_count": snapshot.migration_rejected_rule_count,
        "updated_at": _utc_now(),
    }


def load_profile(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
    legacy_rules: Iterable[Mapping[str, Any]] = (),
    legacy_path: str | Path | None = None,
    migrate: bool = True,
) -> ProfileSnapshot:
    target = _validated_target_directory(target_dir)
    path = profile_path(target, state_root=state_root, profile=profile)
    lock_path = path.with_suffix(".lock")
    for candidate in (path, lock_path):
        _validated_state_document_path(
            candidate,
            state_root=state_root,
            category="profiles",
            relative_parts=2,
            require_exists=False,
        )
    with _ExclusiveFileLock(lock_path):
        if path.exists():
            snapshot = _read_profile_document(path, state_root=state_root)
            if Path(snapshot.target_dir) != target:
                raise SorterV2Error(
                    f"Profile target mismatch: {snapshot.target_dir} != {target}"
                )
            return snapshot

        rules = _clean_rule_dicts(legacy_rules)
        migrated_from = str(Path(legacy_path).resolve()) if legacy_path else None
        snapshot = ProfileSnapshot(
            profile_id=profile_id_for(target, profile),
            profile_name=profile,
            target_dir=str(target),
            revision=0,
            enabled=True,
            duplicate_trash_enabled=False,
            quiet_seconds=DEFAULT_QUIET_SECONDS,
            include=DEFAULT_INCLUDE,
            exclude=DEFAULT_EXCLUDE,
            rules=rules,
            path=path,
            migrated_from=migrated_from if rules else None,
        )
        if migrate:
            _validated_state_document_path(
                path,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=False,
            )
            _atomic_write_json(path, _profile_document(snapshot))
        return snapshot


def save_profile(
    snapshot: ProfileSnapshot,
    *,
    rules: Iterable[Mapping[str, Any]] | None = None,
    enabled: bool | None = None,
    duplicate_trash_enabled: bool | None = None,
    quiet_seconds: float | None = None,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    expected_revision: int | None = None,
    acknowledge_migration_review: bool = False,
) -> ProfileSnapshot:
    path = snapshot.path
    if path.name != "profile.json" or len(path.parents) < 3:
        raise SorterV2Error(f"Invalid profile state path: {path}")
    state_root = path.parents[2]
    target = _validated_target_directory(
        snapshot.target_dir,
        label="Profile target",
    )
    expected_path = profile_path(
        target,
        state_root=state_root,
        profile=snapshot.profile_name,
    )
    if (
        snapshot.profile_id != expected_path.parent.name
        or not _same_path_identity(path, expected_path)
    ):
        raise SorterV2Error(
            f"Profile identity does not match its state path: {path}"
        )
    _validated_state_document_path(
        path,
        state_root=state_root,
        category="profiles",
        relative_parts=2,
        require_exists=False,
    )
    lock_path = path.with_suffix(".lock")
    _validated_state_document_path(
        lock_path,
        state_root=state_root,
        category="profiles",
        relative_parts=2,
        require_exists=False,
    )
    with _ExclusiveFileLock(lock_path):
        if path.exists():
            current = _read_profile_document(path, state_root=state_root)
            current_revision = current.revision
        else:
            current = snapshot
            current_revision = snapshot.revision
        expected = snapshot.revision if expected_revision is None else expected_revision
        if current_revision != expected:
            raise RuleConflictError(
                f"Profile revision changed: expected {expected}, found {current_revision}"
            )
        migration_required_review = (
            current.migration_required_review
            and not acknowledge_migration_review
        )
        next_enabled = current.enabled if enabled is None else bool(enabled)
        if next_enabled and migration_required_review:
            raise SorterV2Error(
                "Legacy rule migration must be reviewed before automatic "
                "classification can be enabled."
            )
        next_snapshot = ProfileSnapshot(
            profile_id=current.profile_id,
            profile_name=current.profile_name,
            target_dir=current.target_dir,
            revision=current_revision + 1,
            enabled=next_enabled,
            duplicate_trash_enabled=(
                current.duplicate_trash_enabled
                if duplicate_trash_enabled is None
                else bool(duplicate_trash_enabled)
            ),
            quiet_seconds=(
                current.quiet_seconds
                if quiet_seconds is None
                else max(0.0, float(quiet_seconds))
            ),
            include=(
                current.include
                if include is None
                else _clean_patterns(include, default=DEFAULT_INCLUDE)
            ),
            exclude=(
                current.exclude
                if exclude is None
                else _clean_patterns(exclude, default=DEFAULT_EXCLUDE)
            ),
            rules=(
                current.rules
                if rules is None
                else _clean_rule_dicts(rules)
            ),
            path=path,
            migrated_from=current.migrated_from,
            migration_required_review=migration_required_review,
            migration_rejected_rule_count=(
                0
                if acknowledge_migration_review
                else current.migration_rejected_rule_count
            ),
        )
        _atomic_write_json(path, _profile_document(next_snapshot))
        return next_snapshot


def list_profiles(
    *,
    state_root: str | Path | None = None,
) -> list[ProfileSnapshot]:
    from ._paths import _state_category_root

    try:
        profiles_dir = _state_category_root(state_root, "profiles")
    except SorterV2Error:
        return []
    if not profiles_dir.is_dir():
        return []
    snapshots: list[ProfileSnapshot] = []
    for path in profiles_dir.glob("*/profile.json"):
        try:
            snapshots.append(
                _read_profile_document(path, state_root=state_root)
            )
        except SorterV2Error:
            continue
    return sorted(
        snapshots,
        key=lambda item: (item.target_dir.casefold(), item.profile_id.casefold()),
    )
