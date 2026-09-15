"""Organize plan preview, execution, and validation for file sorter CLI."""

from __future__ import annotations

import fnmatch
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..infrastructure.sorter_engine import (
    DEFAULT_QUIET_SECONDS,
    OrganizePlan,
    PlanOperation,
    ProfileSnapshot,
    SkippedFile,
    SorterV2Error,
    check_file_stability,
    execute_plan,
    list_profiles,
    load_plan,
    new_plan,
    profile_path,
    same_volume,
    save_plan,
)
from .cli_constants import LEGACY_RULES_FILE_NAME
from .cli_models import FileSorterError, OrganizeResult
from .cli_keywords import build_keyword_rules, keyword_matches
from .cli_paths import (
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



def _preview_source_files(target: Path) -> list[Path]:
    return [
        item
        for item in sorted(target.iterdir(), key=lambda path: normalize_text(path.name))
        if item.is_file() and item.name != LEGACY_RULES_FILE_NAME
    ]



def _plan_operation_for(
    item: Path,
    matched_rule: Any,
    destination_dir: Path,
    destination: Path,
    stability: Any,
) -> PlanOperation:
    return PlanOperation(
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



def _preview_item_skip(item: Path, category: str, reason: str) -> SkippedFile:
    return SkippedFile(source=str(item), category=category, reason=reason)



def _classify_preview_item(
    item: Path,
    *,
    snapshot: ProfileSnapshot,
    rules: list[Any],
    target: Path,
    reserved: set[str],
    quiet_seconds: float,
) -> PlanOperation | SkippedFile:
    if not _matches_profile_patterns(
        item.name,
        include=snapshot.include,
        exclude=snapshot.exclude,
    ):
        return _preview_item_skip(item, "filtered", "profile-pattern")
    stability = check_file_stability(
        item,
        quiet_seconds=max(0.0, quiet_seconds),
    )
    if not stability.stable or stability.fingerprint is None:
        return _preview_item_skip(item, "unstable", stability.reason or "unknown")
    matched_rule = next(
        (rule for rule in rules if keyword_matches(item.stem, rule.keyword)),
        None,
    )
    if matched_rule is None:
        return _preview_item_skip(item, "unmatched", "no-keyword-rule")
    destination_dir = resolve_destination_dir(target, matched_rule.folder)
    destination = _planned_unique_destination(
        destination_dir,
        item.name,
        reserved,
    )
    return _plan_operation_for(
        item,
        matched_rule,
        destination_dir,
        destination,
        stability,
    )



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
    for item in _preview_source_files(target):
        outcome = _classify_preview_item(
            item,
            snapshot=snapshot,
            rules=rules,
            target=target,
            reserved=reserved,
            quiet_seconds=quiet_seconds,
        )
        if isinstance(outcome, SkippedFile):
            skipped.append(outcome)
        else:
            operations.append(outcome)
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



def _validate_plan_operations_against_rules(
    plan: OrganizePlan,
    *,
    effective_root: str | Path | None,
    profile_name: str | None,
) -> None:
    current_rules = build_keyword_rules(
        plan.target_dir,
        state_root=effective_root,
        profile=profile_name,
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



def _validate_plan_profile(
    plan: OrganizePlan,
    *,
    effective_root: str | Path | None,
    profile: str | None,
) -> None:
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
    _validate_plan_operations_against_rules(
        plan,
        effective_root=effective_root,
        profile_name=snapshot.profile_name,
    )



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
            _validate_plan_profile(
                plan,
                effective_root=effective_root,
                profile=profile,
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
