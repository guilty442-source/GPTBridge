"""Profile snapshot loading and legacy profile migration for file sorter CLI."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..infrastructure.sorter_engine import (
    DEFAULT_QUIET_SECONDS,
    ProfileSnapshot,
    RuleConflictError,
    SorterV2Error,
    load_profile,
    profile_path,
)
from ..infrastructure.sorter_engine import (
    _ExclusiveFileLock,
    _atomic_write_json,
    _validated_state_document_path,
)
from .cli_constants import LEGACY_RULES_QUARANTINE_PREFIX
from .cli_models import FileSorterError, KeywordRule
from .cli_paths import (
    _is_lexically_canonical_absolute,
    _profile_id_for_canonical_target,
    resolve_target_dir,
)
from .cli_rules_migration import (
    _legacy_rules_for_migration,
    _migrate_legacy_rule_values,
)


def _stored_profile_identity(
    value: dict[str, Any],
    configured_path: Path,
) -> dict[str, Any]:
    stored_target_text = str(
        value.get("target_dir") or value.get("target") or ""
    ).strip()
    return {
        "target_text": stored_target_text,
        "profile_id": str(
            value.get("profile_id") or configured_path.parent.name
        ).strip(),
        "profile_name": (
            str(value["profile_name"])
            if value.get("profile_name") is not None
            else None
        ),
        "raw_target": Path(stored_target_text).expanduser(),
    }



def _profile_document_identity_matches(
    stored: dict[str, Any],
    configured_path: Path,
    target: Path,
    profile: str | None,
) -> bool:
    return not (
        not stored["target_text"]
        or not _is_lexically_canonical_absolute(stored["raw_target"])
        or os.path.normcase(str(stored["raw_target"]))
        != os.path.normcase(str(target))
        or stored["profile_name"] != profile
        or configured_path.name != "profile.json"
        or stored["profile_id"] != configured_path.parent.name
        or stored["profile_id"]
        != _profile_id_for_canonical_target(stored["raw_target"], stored["profile_name"])
    )



def _quarantine_profile_source(
    configured_path: Path,
    value: dict[str, Any],
    state_root: str | Path | None,
) -> Path:
    source_digest = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    quarantine_path = configured_path.parent / (
        f"{LEGACY_RULES_QUARANTINE_PREFIX}"
        f"{source_digest[:20]}-profile.json"
    )
    _validated_state_document_path(
        quarantine_path,
        state_root=state_root,
        category="profiles",
        relative_parts=2,
        require_exists=False,
    )
    if quarantine_path.exists():
        existing = json.loads(quarantine_path.read_text(encoding="utf-8"))
        if existing != value:
            raise OSError("profile quarantine digest collision")
    else:
        _atomic_write_json(quarantine_path, value)
    return quarantine_path



def _build_migrated_profile_document(
    value: dict[str, Any],
    migration: Any,
    stored: dict[str, Any],
    target: Path,
    quarantine_path: Path,
) -> dict[str, Any]:
    try:
        old_revision = max(0, int(value.get("revision", 0)))
    except (TypeError, ValueError):
        old_revision = 0
    try:
        quiet_seconds = max(
            0.0,
            float(value.get("quiet_seconds", DEFAULT_QUIET_SECONDS)),
        )
    except (TypeError, ValueError):
        quiet_seconds = DEFAULT_QUIET_SECONDS
    raw_include = value.get("include")
    raw_exclude = value.get("exclude")
    return {
        **value,
        "schema_version": 1,
        "profile_id": stored["profile_id"],
        "profile_name": stored["profile_name"],
        "target": str(target),
        "target_dir": str(target),
        "revision": old_revision + 1,
        "enabled": False,
        "quiet_seconds": quiet_seconds,
        "include": (
            [str(item) for item in raw_include if str(item).strip()]
            if isinstance(raw_include, list)
            else ["*"]
        ),
        "exclude": (
            [str(item) for item in raw_exclude if str(item).strip()]
            if isinstance(raw_exclude, list)
            else []
        ),
        "rules": [
            {"keyword": rule.keyword, "folder": rule.folder}
            for rule in migration.rules
        ],
        "migrated_from": str(quarantine_path),
        "migration_required_review": True,
        "migration_rejected_rule_count": len(migration.rejected),
    }



def _migrate_profile_document_locked(
    configured_path: Path,
    *,
    target: Path,
    state_root: str | Path | None,
    profile: str | None,
) -> bool:
    _validated_state_document_path(
        configured_path,
        state_root=state_root,
        category="profiles",
        relative_parts=2,
        require_exists=True,
    )
    value = json.loads(configured_path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("rules"), list):
        return False

    stored = _stored_profile_identity(value, configured_path)
    if not _profile_document_identity_matches(
        stored,
        configured_path,
        target,
        profile,
    ):
        return False

    migration = _migrate_legacy_rule_values(
        value["rules"],
        target,
        require_existing_destination=False,
    )
    if not migration.rejected and migration.converted_count == 0:
        return False

    quarantine_path = _quarantine_profile_source(
        configured_path,
        value,
        state_root,
    )
    _atomic_write_json(
        configured_path,
        _build_migrated_profile_document(
            value,
            migration,
            stored,
            target,
            quarantine_path,
        ),
    )
    return True



def _migrate_existing_profile_rules(
    configured_path: Path,
    *,
    target: Path,
    state_root: str | Path | None,
    profile: str | None,
) -> ProfileSnapshot | None:
    """Recover pre-boundary profiles without re-enabling automatic moves."""

    try:
        lock_path = configured_path.with_suffix(".lock")
        for candidate in (configured_path, lock_path):
            _validated_state_document_path(
                candidate,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=candidate == configured_path,
            )
        with _ExclusiveFileLock(lock_path):
            migrated = _migrate_profile_document_locked(
                configured_path,
                target=target,
                state_root=state_root,
                profile=profile,
            )
        if not migrated:
            return None
    except (
        OSError,
        json.JSONDecodeError,
        RuleConflictError,
        SorterV2Error,
        TypeError,
        ValueError,
    ) as error:
        raise FileSorterError(f"無法安全遷移舊版 profile 規則：{error}") from error

    try:
        return load_profile(
            target,
            state_root=state_root,
            profile=profile,
        )
    except SorterV2Error as error:
        raise FileSorterError(f"無法讀回已遷移的 profile：{error}") from error



def _snapshot_legacy_rules(
    configured_path: Path,
    target: Path,
    state_root: str | Path | None,
    profile: str | None,
) -> tuple[ProfileSnapshot | None, list[KeywordRule], Path | None, int]:
    if configured_path.exists():
        migrated_snapshot = _migrate_existing_profile_rules(
            configured_path,
            target=target,
            state_root=state_root,
            profile=profile,
        )
        return migrated_snapshot, [], None, 0
    legacy_rules, legacy_path, legacy_rejected_count = (
        _legacy_rules_for_migration(
            target,
            configured_path,
            state_root=state_root,
        )
    )
    return None, legacy_rules, legacy_path, legacy_rejected_count



def _verify_new_profile_identity(
    value: Any,
    configured_path: Path,
    target: Path,
    profile: str | None,
    snapshot: ProfileSnapshot,
) -> None:
    if not isinstance(value, dict):
        raise FileSorterError("New profile document is not an object.")
    stored_target = str(
        value.get("target_dir") or value.get("target") or ""
    ).strip()
    stored_profile_id = str(value.get("profile_id") or "").strip()
    if (
        stored_target != str(target)
        or stored_profile_id != snapshot.profile_id
        or configured_path.parent.name != snapshot.profile_id
        or value.get("profile_name") != profile
    ):
        raise FileSorterError(
            "New profile identity changed during legacy migration."
        )



def _mark_review_locked(
    configured_path: Path,
    *,
    target: Path,
    profile: str | None,
    snapshot: ProfileSnapshot,
    legacy_rejected_count: int,
    state_root: str | Path | None,
) -> None:
    _validated_state_document_path(
        configured_path,
        state_root=state_root,
        category="profiles",
        relative_parts=2,
        require_exists=True,
    )
    value = json.loads(configured_path.read_text(encoding="utf-8"))
    _verify_new_profile_identity(
        value, configured_path, target, profile, snapshot
    )
    try:
        old_revision = max(0, int(value.get("revision", 0)))
    except (TypeError, ValueError):
        old_revision = 0
    _atomic_write_json(
        configured_path,
        {
            **value,
            "revision": old_revision + 1,
            "enabled": False,
            "migration_required_review": True,
            "migration_rejected_rule_count": legacy_rejected_count,
        },
    )



def _mark_profile_migration_review(
    configured_path: Path,
    *,
    target: Path,
    profile: str | None,
    snapshot: ProfileSnapshot,
    legacy_rejected_count: int,
    state_root: str | Path | None,
) -> None:
    try:
        lock_path = configured_path.with_suffix(".lock")
        for candidate in (configured_path, lock_path):
            _validated_state_document_path(
                candidate,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=candidate == configured_path,
            )
        with _ExclusiveFileLock(lock_path):
            _mark_review_locked(
                configured_path,
                target=target,
                profile=profile,
                snapshot=snapshot,
                legacy_rejected_count=legacy_rejected_count,
                state_root=state_root,
            )
    except (OSError, json.JSONDecodeError, SorterV2Error) as error:
        raise FileSorterError(
            f"Cannot persist legacy migration review state: {error}"
        ) from error



def _load_snapshot_or_raise(
    target: Path,
    *,
    state_root: str | Path | None,
    profile: str | None,
    legacy_rules: list[KeywordRule],
    legacy_path: Path | None,
) -> ProfileSnapshot:
    try:
        return load_profile(
            target,
            state_root=state_root,
            profile=profile,
            legacy_rules=(
                {"keyword": rule.keyword, "folder": rule.folder}
                for rule in legacy_rules
                if rule.source == "custom"
            ),
            legacy_path=legacy_path,
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error



def _load_profile_snapshot(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> ProfileSnapshot:
    target = resolve_target_dir(target_dir)
    configured_path = profile_path(target, state_root=state_root, profile=profile)
    (
        migrated_snapshot,
        legacy_rules,
        legacy_path,
        legacy_rejected_count,
    ) = _snapshot_legacy_rules(configured_path, target, state_root, profile)
    if migrated_snapshot is not None:
        return migrated_snapshot
    snapshot = _load_snapshot_or_raise(
        target,
        state_root=state_root,
        profile=profile,
        legacy_rules=legacy_rules,
        legacy_path=legacy_path,
    )
    if legacy_rejected_count <= 0:
        return snapshot

    _mark_profile_migration_review(
        configured_path,
        target=target,
        profile=profile,
        snapshot=snapshot,
        legacy_rejected_count=legacy_rejected_count,
        state_root=state_root,
    )
    try:
        return load_profile(target, state_root=state_root, profile=profile)
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error
