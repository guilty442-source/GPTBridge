"""Organize plan preview, execution, and automation for file sorter CLI."""

from __future__ import annotations

import fnmatch
import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..infrastructure.cleanup import (
    CleanupError,
    DEFAULT_ANALYSIS_SPEED,
    DEFAULT_SIMILAR_VIDEO_THRESHOLD,
    find_exact_duplicate_candidates,
    print_progress_event,
    recycle_exact_duplicate_candidates,
    run_cleanup_scan,
)
from ..infrastructure.sorter_engine import (
    DEFAULT_QUIET_SECONDS,
    OrganizePlan,
    PlanOperation,
    ProfileSnapshot,
    RuleConflictError,
    SkippedFile,
    SorterV2Error,
    check_file_stability,
    execute_plan,
    list_profiles,
    load_plan,
    load_profile,
    new_plan,
    profile_path,
    prune_state,
    recover_transactions,
    resolve_state_root,
    same_volume,
    save_plan,
    save_profile,
    transaction_history,
    undo_last_transaction,
)
from ..infrastructure.sorter_engine import (
    _TargetDirectoryLock,
    _atomic_write_json,
    _validated_state_document_path,
    _utc_now,
    _state_category_root,
)
from .cli_constants import (
    FOLDERS_JSON_PREFIX,
    LEGACY_RULES_FILE_NAME,
    SOURCE_FILES_JSON_PREFIX,
    TOOL_ROOT,
    _BACKGROUND_DUPLICATE_OBSERVATIONS,
    _BACKGROUND_OBSERVATIONS,
    _BACKGROUND_OBSERVATIONS_LOCK,
)
from .cli_models import FileSorterError, KeywordRule, OrganizeResult
from .cli_keywords import build_keyword_rules, keyword_matches
from .cli_paths import (
    _is_lexically_canonical_absolute,
    is_local_folder_name,
    list_destination_folders,
    list_source_files,
    normalize_match_text,
    normalize_text,
    resolve_destination_dir,
    resolve_target_dir,
)
from .cli_rules import (
    _load_profile_snapshot,
    _uses_explicit_legacy_rules_path,
    get_rules_path,
    read_custom_rules,
)


def _facade_override(name: str, default: Any) -> Any:
    """Honor public-facade injection without duplicating runtime ownership."""

    facade = sys.modules.get(f"{__package__}.cli")
    return getattr(facade, name, default) if facade is not None else default


def _effective_state_root(
    state_root: str | Path | None,
) -> str | Path | None:
    if state_root is not None or not _uses_explicit_legacy_rules_path():
        return state_root
    return get_rules_path().parent / ".file-sorter-v1-state"



def _profile_snapshot_for_plan(
    target: Path,
    *,
    state_root: str | Path | None,
    profile: str | None,
) -> ProfileSnapshot:
    if not _uses_explicit_legacy_rules_path():
        return _load_profile_snapshot(
            target,
            state_root=state_root,
            profile=profile,
        )
    return ProfileSnapshot(
        profile_id=profile_path(
            target,
            state_root=_effective_state_root(state_root),
            profile=profile,
        ).parent.name,
        profile_name=profile,
        target_dir=str(target),
        revision=0,
        enabled=True,
        duplicate_trash_enabled=False,
        quiet_seconds=DEFAULT_QUIET_SECONDS,
        include=("*",),
        exclude=(),
        rules=tuple(
            {"keyword": rule.keyword, "folder": rule.folder}
            for rule in read_custom_rules()
        ),
        path=get_rules_path(),
    )



def _matches_profile_patterns(
    name: str,
    *,
    include: Iterable[str],
    exclude: Iterable[str],
) -> bool:
    normalized_name = normalize_text(name)
    included = any(
        fnmatch.fnmatchcase(normalized_name, normalize_text(pattern))
        for pattern in include
    )
    excluded = any(
        fnmatch.fnmatchcase(normalized_name, normalize_text(pattern))
        for pattern in exclude
    )
    return included and not excluded



def _planned_unique_destination(
    destination_dir: Path,
    file_name: str,
    reserved: set[str],
) -> Path:
    source_name = Path(file_name)
    for index in range(100_000):
        candidate = (
            destination_dir / file_name
            if index == 0
            else destination_dir / f"{source_name.stem}_{index}{source_name.suffix}"
        )
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key not in reserved and not candidate.exists():
            reserved.add(key)
            return candidate
    raise FileSorterError(f"Unable to allocate a unique destination for {file_name}")



def preview_organize_files(
    target_dir: str | Path,
    *,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    state_root: str | Path | None = None,
    profile: str | None = None,
    persist: bool = True,
) -> OrganizePlan:
    target = resolve_target_dir(target_dir)
    effective_root = _effective_state_root(state_root)
    snapshot = _profile_snapshot_for_plan(
        target,
        state_root=effective_root,
        profile=profile,
    )
    rules = build_keyword_rules(
        target,
        state_root=effective_root,
        profile=profile,
    )
    operations: list[PlanOperation] = []
    skipped: list[SkippedFile] = []
    reserved: set[str] = set()
    files = [
        item
        for item in sorted(target.iterdir(), key=lambda path: normalize_text(path.name))
        if item.is_file() and item.name != LEGACY_RULES_FILE_NAME
    ]
    for item in files:
        if not _matches_profile_patterns(
            item.name,
            include=snapshot.include,
            exclude=snapshot.exclude,
        ):
            skipped.append(
                SkippedFile(
                    source=str(item),
                    category="filtered",
                    reason="profile-pattern",
                )
            )
            continue
        stability = check_file_stability(
            item,
            quiet_seconds=max(0.0, quiet_seconds),
        )
        if not stability.stable or stability.fingerprint is None:
            skipped.append(
                SkippedFile(
                    source=str(item),
                    category="unstable",
                    reason=stability.reason or "unknown",
                )
            )
            continue
        matched_rule = next(
            (rule for rule in rules if keyword_matches(item.stem, rule.keyword)),
            None,
        )
        if matched_rule is None:
            skipped.append(
                SkippedFile(
                    source=str(item),
                    category="unmatched",
                    reason="no-keyword-rule",
                )
            )
            continue
        destination_dir = resolve_destination_dir(target, matched_rule.folder)
        destination = _planned_unique_destination(
            destination_dir,
            item.name,
            reserved,
        )
        operations.append(
            PlanOperation(
                operation_id=str(uuid.uuid4()),
                source=str(item),
                destination=str(destination),
                keyword=matched_rule.keyword,
                folder=matched_rule.folder,
                rule_source=matched_rule.source,
                source_size=stability.fingerprint.size,
                source_mtime_ns=stability.fingerprint.mtime_ns,
                transfer=(
                    "same-volume"
                    if same_volume(item, destination_dir)
                    else "cross-volume"
                ),
            )
        )
    plan = new_plan(
        target,
        profile_id=snapshot.profile_id,
        rules_revision=snapshot.revision,
        quiet_seconds=quiet_seconds,
        operations=operations,
        skipped=skipped,
    )
    if persist:
        try:
            save_plan(plan, state_root=effective_root)
        except SorterV2Error as error:
            raise FileSorterError(str(error)) from error
    return plan



def apply_organize_plan(
    plan_id: str,
    *,
    target_dir: str | Path | None = None,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    effective_root = _effective_state_root(state_root)
    try:
        plan = load_plan(plan_id, state_root=effective_root)
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error
    if target_dir is not None:
        target = resolve_target_dir(target_dir)
        if Path(plan.target_dir).resolve() != target:
            raise FileSorterError(
                f"Plan target mismatch: {plan.target_dir} != {target}"
            )
    validate_profile = None
    if not _uses_explicit_legacy_rules_path():
        def validate_profile() -> None:
            """Revalidate rules while execute_plan holds the target lock."""

            snapshot = next(
                (
                    item
                    for item in list_profiles(state_root=effective_root)
                    if item.profile_id == plan.profile_id
                    and Path(item.target_dir).resolve()
                    == Path(plan.target_dir).resolve()
                ),
                None,
            )
            if snapshot is None:
                raise FileSorterError(
                    "The profile used by this plan no longer exists."
                )
            if profile is not None:
                selected_path = profile_path(
                    plan.target_dir,
                    state_root=effective_root,
                    profile=profile,
                )
                if selected_path.parent.name != snapshot.profile_id:
                    raise FileSorterError(
                        "Plan profile no longer matches the selected profile."
                    )
            if snapshot.revision != plan.rules_revision:
                raise FileSorterError(
                    "Rules changed after preview; create a new plan before applying."
                )
            current_rules = build_keyword_rules(
                plan.target_dir,
                state_root=effective_root,
                profile=snapshot.profile_name,
            )
            allowed_rules = {
                (
                    normalize_match_text(rule.keyword),
                    os.path.normcase(
                        str(
                            resolve_destination_dir(
                                Path(plan.target_dir).resolve(),
                                rule.folder,
                            )
                        )
                    ),
                )
                for rule in current_rules
            }
            for operation in plan.operations:
                try:
                    operation_destination = os.path.normcase(
                        str(
                            resolve_destination_dir(
                                Path(plan.target_dir).resolve(),
                                operation.folder,
                            )
                        )
                    )
                except FileSorterError as error:
                    raise FileSorterError(
                        "A destination rule in the plan is no longer valid."
                    ) from error
                if (
                    normalize_match_text(operation.keyword),
                    operation_destination,
                ) not in allowed_rules:
                    raise FileSorterError(
                        "A plan operation no longer matches the active profile rules."
                    )
    try:
        return execute_plan(
            plan,
            state_root=effective_root,
            pre_execute_validate=validate_profile,
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error



def organize_files(
    target_dir: str | Path,
    *,
    quiet_seconds: float = 0.0,
    dry_run: bool = False,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> OrganizeResult:
    """Compatibility API backed by a V2 preview and durable transaction."""

    target = resolve_target_dir(target_dir)
    effective_root = _effective_state_root(state_root)
    rules = build_keyword_rules(
        target,
        state_root=effective_root,
        profile=profile,
    )
    result = OrganizeResult(rules=rules)
    if not rules:
        result.errors.append("No usable keyword rules or destination folders were found.")
        return result
    plan = preview_organize_files(
        target,
        quiet_seconds=quiet_seconds,
        state_root=effective_root,
        profile=profile,
        persist=True,
    )
    result.plan = plan.to_dict()
    result.unmatched_count = sum(
        1 for item in plan.skipped if item.category == "unmatched"
    )
    for item in plan.skipped:
        if item.category == "unstable":
            result.warnings.append(
                f"Skipped unstable file {Path(item.source).name}: {item.reason}"
            )
    if dry_run:
        return result
    try:
        execution = execute_plan(plan, state_root=effective_root)
    except SorterV2Error as error:
        result.errors.append(str(error))
        return result
    result.moved_count = int(execution["moved_count"])
    result.errors.extend(str(item) for item in execution["errors"])
    result.transaction_id = str(execution["transaction_id"])
    result.journal_path = str(execution["journal_path"])
    return result



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
        return save_profile(
            snapshot,
            enabled=enabled,
            acknowledge_migration_review=(
                enabled and snapshot.migration_required_review
            ),
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error



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
        try:
            _validated_state_document_path(
                configured_path,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=True,
            )
        except SorterV2Error:
            continue
        try:
            value = json.loads(configured_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or not isinstance(value.get("rules"), list):
            continue
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
        raw_target = Path(target_text).expanduser()
        try:
            identity_valid = (
                configured_path.name == "profile.json"
                and _is_lexically_canonical_absolute(raw_target)
                and profile_id == configured_path.parent.name
                and profile_id
                == _profile_id_for_canonical_target(raw_target, profile_name)
            )
        except FileSorterError:
            identity_valid = False
        if not identity_valid:
            reports.append(
                {
                    "ok": False,
                    "profile_id": profile_id,
                    "target_dir": target_text,
                    "moved_count": 0,
                    "errors": [
                        "Profile identity is invalid; target was not accessed."
                    ],
                    "warnings": [],
                    "migration_required_review": True,
                }
            )
            continue
        try:
            target = resolve_target_dir(target_text)
            migrated = _migrate_existing_profile_rules(
                configured_path,
                target=target,
                state_root=state_root,
                profile=profile_name,
            )
        except FileSorterError as error:
            reports.append(
                {
                    "ok": False,
                    "profile_id": profile_id,
                    "target_dir": target_text,
                    "moved_count": 0,
                    "errors": [str(error)],
                    "warnings": [],
                    "migration_required_review": True,
                }
            )
            continue
        if migrated is None:
            if value.get("migration_required_review") is True:
                reports.append(
                    {
                        "ok": True,
                        "profile_id": profile_id,
                        "target_dir": target_text,
                        "moved_count": 0,
                        "errors": [],
                        "warnings": [
                            "Legacy rule migration is waiting for user review; "
                            "automatic classification remains disabled."
                        ],
                        "migration_required_review": True,
                    }
                )
            continue
        reports.append(
            {
                "ok": True,
                "profile_id": migrated.profile_id,
                "target_dir": migrated.target_dir,
                "moved_count": 0,
                "errors": [],
                "warnings": [
                    "Legacy destination rules were quarantined; "
                    "automatic classification remains disabled pending review."
                ],
                "migration_required_review": True,
            }
        )
    return reports



def _record_duplicate_recycle_result(
    result: dict[str, Any],
    *,
    profile_id: str,
    state_root: str | Path | None,
) -> str | None:
    if int(result.get("recycled_count", 0)) <= 0:
        return None
    transaction_id = str(uuid.uuid4())
    path = (
        resolve_state_root(state_root)
        / "recycle-journals"
        / f"{transaction_id}.json"
    )
    _validated_state_document_path(
        path,
        state_root=state_root,
        category="recycle-journals",
        relative_parts=1,
        require_exists=False,
    )
    _atomic_write_json(
        path,
        {
            "schema_version": 1,
            "type": "file-sorter-duplicate-recycle-journal",
            "transaction_id": transaction_id,
            "profile_id": profile_id,
            "created_at": _utc_now(),
            **result,
        },
    )
    return transaction_id



def _run_profile_duplicate_recycle_once(
    snapshot: ProfileSnapshot,
    *,
    effective_root: str | Path | None,
    observation_key: tuple[str, str],
) -> dict[str, Any]:
    candidates = find_exact_duplicate_candidates(
        snapshot.target_dir,
        quiet_seconds=snapshot.quiet_seconds,
    )
    current_observations = {
        str(item["path"]): (
            int(item["size"]),
            int(item["mtime_ns"]),
            str(item["sha256"]),
            str(item["keep_path"]),
        )
        for item in candidates
    }
    with _BACKGROUND_OBSERVATIONS_LOCK:
        previous = _BACKGROUND_DUPLICATE_OBSERVATIONS.get(observation_key, {})
        _BACKGROUND_DUPLICATE_OBSERVATIONS[observation_key] = current_observations
    ready = [
        item
        for item in candidates
        if previous.get(str(item["path"]))
        == current_observations[str(item["path"])]
    ]
    result: dict[str, Any] = {
        "ok": True,
        "recycled_count": 0,
        "recycled": [],
        "errors": [],
    }
    if ready:
        with _TargetDirectoryLock(snapshot.target_dir):
            recycler = _facade_override(
                "recycle_exact_duplicate_candidates",
                recycle_exact_duplicate_candidates,
            )
            result = recycler(snapshot.target_dir, ready)
    transaction_id = _record_duplicate_recycle_result(
        result,
        profile_id=snapshot.profile_id,
        state_root=effective_root,
    )
    return {
        "recycled_count": int(result.get("recycled_count", 0)),
        "duplicate_waiting_for_second_observation_count": (
            len(current_observations) - len(ready)
        ),
        "duplicate_errors": [str(item) for item in result.get("errors", [])],
        "recycle_transaction_id": transaction_id,
    }



def _run_profile_classification_once(
    snapshot: ProfileSnapshot,
    *,
    effective_root: str | Path | None,
    observation_key: tuple[str, str],
) -> dict[str, Any]:
    plan = preview_organize_files(
        snapshot.target_dir,
        quiet_seconds=snapshot.quiet_seconds,
        state_root=effective_root,
        profile=snapshot.profile_name,
        persist=False,
    )
    current_observations = {
        operation.source: (
            operation.source_size,
            operation.source_mtime_ns,
        )
        for operation in plan.operations
    }
    with _BACKGROUND_OBSERVATIONS_LOCK:
        previous_observations = _BACKGROUND_OBSERVATIONS.get(observation_key, {})
        _BACKGROUND_OBSERVATIONS[observation_key] = current_observations
    ready_sources = {
        source
        for source, fingerprint in current_observations.items()
        if previous_observations.get(source) == fingerprint
    }
    plan.operations = [
        operation
        for operation in plan.operations
        if operation.source in ready_sources
    ]
    warnings = [
        f"Skipped unstable file {Path(item.source).name}: {item.reason}"
        for item in plan.skipped
        if item.category == "unstable"
    ]
    transaction_id: str | None = None
    errors: list[str] = []
    moved_count = 0
    if plan.operations:
        save_plan(plan, state_root=effective_root)
        execution = apply_organize_plan(
            plan.plan_id,
            target_dir=snapshot.target_dir,
            state_root=effective_root,
            profile=snapshot.profile_name,
        )
        moved_count = int(execution["moved_count"])
        errors = [str(item) for item in execution["errors"]]
        transaction_id = str(execution["transaction_id"])
    return {
        "moved_count": moved_count,
        "unmatched_count": sum(
            1 for item in plan.skipped if item.category == "unmatched"
        ),
        "waiting_for_second_observation_count": (
            len(current_observations) - len(ready_sources)
        ),
        "classification_errors": errors,
        "warnings": warnings,
        "transaction_id": transaction_id,
    }



def run_enabled_profiles_once(
    *,
    state_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Run explicitly enabled classification and duplicate-recycle profiles."""

    effective_root = _effective_state_root(state_root)
    reports = _migrate_legacy_profiles_for_automation(effective_root)
    root_key = str(resolve_state_root(effective_root))
    active_classification_keys: set[tuple[str, str]] = set()
    active_duplicate_keys: set[tuple[str, str]] = set()
    for snapshot in list_profiles(state_root=effective_root):
        if snapshot.migration_required_review:
            continue
        if not snapshot.enabled and not snapshot.duplicate_trash_enabled:
            continue
        observation_key = (root_key, snapshot.profile_id)
        report: dict[str, Any] = {
            "ok": True,
            "profile_id": snapshot.profile_id,
            "target_dir": snapshot.target_dir,
            "moved_count": 0,
            "recycled_count": 0,
            "unmatched_count": 0,
            "waiting_for_second_observation_count": 0,
            "duplicate_waiting_for_second_observation_count": 0,
            "errors": [],
            "warnings": [],
            "transaction_id": None,
            "recycle_transaction_id": None,
        }
        try:
            if snapshot.duplicate_trash_enabled:
                active_duplicate_keys.add(observation_key)
                duplicate_report = _run_profile_duplicate_recycle_once(
                    snapshot,
                    effective_root=effective_root,
                    observation_key=observation_key,
                )
                report.update(duplicate_report)
                report["errors"].extend(duplicate_report["duplicate_errors"])
            if snapshot.enabled:
                active_classification_keys.add(observation_key)
                classification_report = _run_profile_classification_once(
                    snapshot,
                    effective_root=effective_root,
                    observation_key=observation_key,
                )
                report.update(classification_report)
                report["errors"].extend(
                    classification_report["classification_errors"]
                )
            report["ok"] = not report["errors"]
        except (CleanupError, FileSorterError, SorterV2Error, OSError) as error:
            report["ok"] = False
            report["errors"].append(str(error))
        reports.append(report)

    with _BACKGROUND_OBSERVATIONS_LOCK:
        for observation_key in list(_BACKGROUND_OBSERVATIONS):
            if (
                observation_key[0] == root_key
                and observation_key not in active_classification_keys
            ):
                _BACKGROUND_OBSERVATIONS.pop(observation_key, None)
        for observation_key in list(_BACKGROUND_DUPLICATE_OBSERVATIONS):
            if (
                observation_key[0] == root_key
                and observation_key not in active_duplicate_keys
            ):
                _BACKGROUND_DUPLICATE_OBSERVATIONS.pop(observation_key, None)
    return reports



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
