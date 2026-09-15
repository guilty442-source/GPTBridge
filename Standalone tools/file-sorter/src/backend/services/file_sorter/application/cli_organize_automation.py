"""Background profile automation runs for file sorter CLI."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

from ..infrastructure.cleanup import (
    CleanupError,
    find_exact_duplicate_candidates,
    recycle_exact_duplicate_candidates,
)
from ..infrastructure.sorter_engine import (
    ProfileSnapshot,
    SorterV2Error,
    list_profiles,
    resolve_state_root,
    save_plan,
)
from ..infrastructure.sorter_engine import (
    _TargetDirectoryLock,
    _atomic_write_json,
    _state_category_root,
    _utc_now,
    _validated_state_document_path,
)
from .cli_constants import (
    _BACKGROUND_DUPLICATE_OBSERVATIONS,
    _BACKGROUND_OBSERVATIONS,
    _BACKGROUND_OBSERVATIONS_LOCK,
)
from .cli_models import FileSorterError
from .cli_organize_migration import (
    _migrate_legacy_profiles_for_automation,
)
from .cli_organize_plan import (
    _effective_state_root,
    _facade_override,
    apply_organize_plan,
    preview_organize_files,
)

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



def _observed_duplicate_fingerprints(
    candidates: list[dict[str, Any]],
) -> dict[str, tuple[int, int, str, str]]:
    return {
        str(item["path"]): (
            int(item["size"]),
            int(item["mtime_ns"]),
            str(item["sha256"]),
            str(item["keep_path"]),
        )
        for item in candidates
    }



def _recycle_ready_duplicates(
    snapshot: ProfileSnapshot,
    ready: list[dict[str, Any]],
) -> dict[str, Any]:
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
    return result



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
    current_observations = _observed_duplicate_fingerprints(candidates)
    with _BACKGROUND_OBSERVATIONS_LOCK:
        previous = _BACKGROUND_DUPLICATE_OBSERVATIONS.get(observation_key, {})
        _BACKGROUND_DUPLICATE_OBSERVATIONS[observation_key] = current_observations
    ready = [
        item
        for item in candidates
        if previous.get(str(item["path"]))
        == current_observations[str(item["path"])]
    ]
    result = _recycle_ready_duplicates(snapshot, ready)
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



def _confirmed_plan_operations(
    plan: Any,
    observation_key: tuple[str, str],
) -> tuple[dict[str, tuple[int, int]], set[str]]:
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
    return current_observations, ready_sources



def _execute_confirmed_plan(
    plan: Any,
    snapshot: ProfileSnapshot,
    effective_root: str | Path | None,
) -> tuple[int, list[str], str | None]:
    if not plan.operations:
        return 0, [], None
    save_plan(plan, state_root=effective_root)
    execution = apply_organize_plan(
        plan.plan_id,
        target_dir=snapshot.target_dir,
        state_root=effective_root,
        profile=snapshot.profile_name,
    )
    return (
        int(execution["moved_count"]),
        [str(item) for item in execution["errors"]],
        str(execution["transaction_id"]),
    )



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
    current_observations, ready_sources = _confirmed_plan_operations(
        plan,
        observation_key,
    )
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
    moved_count, errors, transaction_id = _execute_confirmed_plan(
        plan,
        snapshot,
        effective_root,
    )
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



def _profile_once_report(snapshot: ProfileSnapshot) -> dict[str, Any]:
    return {
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



def _run_profile_once(
    snapshot: ProfileSnapshot,
    *,
    effective_root: str | Path | None,
    observation_key: tuple[str, str],
    active_classification_keys: set[tuple[str, str]],
    active_duplicate_keys: set[tuple[str, str]],
) -> dict[str, Any]:
    report = _profile_once_report(snapshot)
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
    return report



def _prune_inactive_observations(
    root_key: str,
    active_classification_keys: set[tuple[str, str]],
    active_duplicate_keys: set[tuple[str, str]],
) -> None:
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
        reports.append(
            _run_profile_once(
                snapshot,
                effective_root=effective_root,
                observation_key=observation_key,
                active_classification_keys=active_classification_keys,
                active_duplicate_keys=active_duplicate_keys,
            )
        )
    _prune_inactive_observations(
        root_key,
        active_classification_keys,
        active_duplicate_keys,
    )
    return reports
