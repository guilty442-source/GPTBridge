"""Plan creation, persistence, and state pruning."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from ._constants import (
    DEFAULT_JOURNAL_RETENTION_DAYS,
    JOURNAL_RETENTION_DAYS_ENV,
    SCHEMA_VERSION,
    TERMINAL_TRANSACTION_STATES,
    SorterV2Error,
)
from ._io_utils import _atomic_write_json
from ._models import OrganizePlan, PlanOperation, SkippedFile, _plan_expired, _utc_now
from ._paths import (
    _same_path_identity,
    _state_category_root,
    _validated_id,
    _validated_state_document_path,
    _validated_target_directory,
    _validate_operation_paths,
    resolve_state_root,
)


def new_plan(
    target_dir: str | Path,
    *,
    profile_id: str,
    rules_revision: int,
    quiet_seconds: float,
    operations: Iterable[PlanOperation],
    skipped: Iterable[SkippedFile],
) -> OrganizePlan:
    target = _validated_target_directory(target_dir)
    return OrganizePlan(
        plan_id=str(uuid.uuid4()),
        target_dir=str(target),
        profile_id=profile_id,
        rules_revision=rules_revision,
        quiet_seconds=max(0.0, float(quiet_seconds)),
        operations=list(operations),
        skipped=list(skipped),
    )


def save_plan(
    plan: OrganizePlan,
    *,
    state_root: str | Path | None = None,
) -> Path:
    root = resolve_state_root(state_root)
    target = _validated_target_directory(
        plan.target_dir,
        label="Plan target",
    )
    if not _same_path_identity(Path(plan.target_dir), target):
        raise SorterV2Error("Plan target must be canonical before it is saved.")
    for operation in plan.operations:
        _validate_operation_paths(target, operation)
    path = root / "plans" / f"{_validated_id(plan.plan_id, 'plan id')}.json"
    _validated_state_document_path(
        path,
        state_root=state_root,
        category="plans",
        relative_parts=1,
        require_exists=False,
    )
    if path.exists():
        raise SorterV2Error(f"Plan already exists: {plan.plan_id}")
    _atomic_write_json(path, plan.to_dict())
    return path


def load_plan(
    plan_id: str,
    *,
    state_root: str | Path | None = None,
) -> OrganizePlan:
    path = (
        resolve_state_root(state_root)
        / "plans"
        / f"{_validated_id(plan_id, 'plan id')}.json"
    )
    path = _validated_state_document_path(
        path,
        state_root=state_root,
        category="plans",
        relative_parts=1,
        require_exists=True,
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SorterV2Error(f"Cannot load plan {plan_id}: {error}") from error
    if not isinstance(value, dict):
        raise SorterV2Error(f"Invalid plan document: {path}")
    plan = OrganizePlan.from_dict(value)
    if plan.plan_id != path.stem:
        raise SorterV2Error(f"Plan identity does not match its state path: {path}")
    raw_target = Path(plan.target_dir).expanduser()
    if not raw_target.is_absolute():
        raise SorterV2Error(f"Plan target is not absolute: {path}")
    canonical_target = raw_target.resolve(strict=False)
    if not _same_path_identity(raw_target, canonical_target):
        raise SorterV2Error(f"Plan target is not canonical: {path}")
    return plan


def _journal_retention_days() -> int:
    raw = str(os.environ.get(JOURNAL_RETENTION_DAYS_ENV) or "").strip()
    if not raw:
        return DEFAULT_JOURNAL_RETENTION_DAYS
    try:
        return max(0, min(3650, int(raw)))
    except ValueError:
        return DEFAULT_JOURNAL_RETENTION_DAYS


def _document_age_days(value: Mapping[str, Any], path: Path) -> float:
    timestamp = str(value.get("updated_at") or value.get("created_at") or "")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        moment = parsed.astimezone(timezone.utc)
    except (AttributeError, ValueError):
        moment = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - moment).total_seconds() / 86400)


def prune_state(
    *,
    state_root: str | Path | None = None,
    journal_retention_days: int | None = None,
) -> dict[str, int]:
    """Remove unusable expired plans and optionally old terminal journals.

    Journal retention is disabled by default because journals preserve undo and
    audit information. Set ``FILE_SORTER_JOURNAL_RETENTION_DAYS`` or pass an
    explicit value to opt into terminal-journal cleanup.
    """

    removed_plans = 0
    removed_journals = 0
    removed_recycle_journals = 0
    plans_dir = _state_category_root(state_root, "plans")
    if plans_dir.is_dir():
        for path in plans_dir.glob("*.json"):
            try:
                plan = load_plan(path.stem, state_root=state_root)
                if not _plan_expired(plan):
                    continue
                validated = _validated_state_document_path(
                    path,
                    state_root=state_root,
                    category="plans",
                    relative_parts=1,
                    require_exists=True,
                )
                validated.unlink()
                removed_plans += 1
            except (OSError, SorterV2Error):
                continue

    retention_days = (
        _journal_retention_days()
        if journal_retention_days is None
        else max(0, min(3650, int(journal_retention_days)))
    )
    if retention_days > 0:
        for category in ("journals", "recycle-journals"):
            category_root = _state_category_root(state_root, category)
            if not category_root.is_dir():
                continue
            for path in category_root.glob("*.json"):
                try:
                    validated = _validated_state_document_path(
                        path,
                        state_root=state_root,
                        category=category,
                        relative_parts=1,
                        require_exists=True,
                    )
                    value = json.loads(validated.read_text(encoding="utf-8"))
                    if not isinstance(value, dict):
                        continue
                    if category == "journals" and str(value.get("status")) not in (
                        TERMINAL_TRANSACTION_STATES | {"completed_with_errors"}
                    ):
                        continue
                    if _document_age_days(value, validated) < retention_days:
                        continue
                    validated.unlink()
                    if category == "journals":
                        removed_journals += 1
                    else:
                        removed_recycle_journals += 1
                except (OSError, TypeError, ValueError, json.JSONDecodeError, SorterV2Error):
                    continue
    return {
        "removed_plans": removed_plans,
        "removed_journals": removed_journals,
        "removed_recycle_journals": removed_recycle_journals,
    }
