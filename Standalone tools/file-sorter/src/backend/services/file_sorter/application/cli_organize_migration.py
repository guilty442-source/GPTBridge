"""Legacy profile migration reports for file sorter automation runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..infrastructure.sorter_engine import (
    SorterV2Error,
    _state_category_root,
    _validated_state_document_path,
)
from .cli_models import FileSorterError
from .cli_paths import (
    _is_lexically_canonical_absolute,
    _profile_id_for_canonical_target,
    resolve_target_dir,
)
from .cli_rules import _migrate_existing_profile_rules


def _automation_migration_report(
    ok: bool,
    *,
    profile_id: str,
    target_dir: str,
    errors: tuple[str, ...] = (),
    warnings: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "ok": ok,
        "profile_id": profile_id,
        "target_dir": target_dir,
        "moved_count": 0,
        "errors": list(errors),
        "warnings": list(warnings),
        "migration_required_review": True,
    }



def _automation_profile_identity_valid(
    configured_path: Path,
    raw_target: Path,
    profile_id: str,
    profile_name: str | None,
) -> bool:
    try:
        return bool(
            configured_path.name == "profile.json"
            and _is_lexically_canonical_absolute(raw_target)
            and profile_id == configured_path.parent.name
            and profile_id
            == _profile_id_for_canonical_target(raw_target, profile_name)
        )
    except FileSorterError:
        return False



def _read_automation_profile_document(
    configured_path: Path,
    state_root: str | Path | None,
) -> dict[str, Any] | None:
    try:
        _validated_state_document_path(
            configured_path,
            state_root=state_root,
            category="profiles",
            relative_parts=2,
            require_exists=True,
        )
    except SorterV2Error:
        return None
    try:
        value = json.loads(configured_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("rules"), list):
        return None
    return value



def _attempt_profile_migration(
    configured_path: Path,
    state_root: str | Path | None,
    value: dict[str, Any],
    target_text: str,
    profile_name: str | None,
    profile_id: str,
) -> dict[str, Any] | None:
    try:
        target = resolve_target_dir(target_text)
        migrated = _migrate_existing_profile_rules(
            configured_path,
            target=target,
            state_root=state_root,
            profile=profile_name,
        )
    except FileSorterError as error:
        return _automation_migration_report(
            False, profile_id=profile_id, target_dir=target_text,
            errors=(str(error),),
        )
    if migrated is None:
        if value.get("migration_required_review") is True:
            return _automation_migration_report(
                True, profile_id=profile_id, target_dir=target_text,
                warnings=(
                    "Legacy rule migration is waiting for user review; "
                    "automatic classification remains disabled.",
                ),
            )
        return None
    return _automation_migration_report(
        True,
        profile_id=migrated.profile_id,
        target_dir=migrated.target_dir,
        warnings=(
            "Legacy destination rules were quarantined; "
            "automatic classification remains disabled pending review.",
        ),
    )



def _migrate_legacy_profile_for_automation(
    configured_path: Path,
    state_root: str | Path | None,
) -> dict[str, Any] | None:
    value = _read_automation_profile_document(configured_path, state_root)
    if value is None:
        return None
    target_text = str(
        value.get("target_dir") or value.get("target") or ""
    ).strip()
    profile_name = (
        str(value["profile_name"])
        if value.get("profile_name") is not None
        else None
    )
    profile_id = str(
        value.get("profile_id") or configured_path.parent.name
    ).strip()
    if not _automation_profile_identity_valid(
        configured_path,
        Path(target_text).expanduser(),
        profile_id,
        profile_name,
    ):
        return _automation_migration_report(
            False, profile_id=profile_id, target_dir=target_text,
            errors=("Profile identity is invalid; target was not accessed.",),
        )
    return _attempt_profile_migration(
        configured_path,
        state_root,
        value,
        target_text,
        profile_name,
        profile_id,
    )



def _migrate_legacy_profiles_for_automation(
    state_root: str | Path | None,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    try:
        profiles_dir = _state_category_root(state_root, "profiles")
    except SorterV2Error:
        return reports
    if not profiles_dir.is_dir():
        return reports

    for configured_path in profiles_dir.glob("*/profile.json"):
        report = _migrate_legacy_profile_for_automation(
            configured_path,
            state_root,
        )
        if report is not None:
            reports.append(report)
    return reports
