"""Durable, no-overwrite file operations and per-target state for File Sorter.

The module intentionally has no dependency on ``main.py``.  This keeps the
transaction and profile repository usable by a future background service.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence




from .sorter_journal import (
    _Journal,
    _read_journal,
)
from .sorter_locks import (
    _TargetDirectoryLock,
)
from .sorter_metadata import (
    _ensure_source_unchanged,
    _unlink_file_preserving_failure,
)
from .sorter_paths import (
    _plan_expired,
    _state_category_root,
    _validate_operation_paths,
    _validated_target_directory,
)
from .sorter_stability import (
    check_file_stability,
    same_volume,
)
from .sorter_staging import (
    _same_volume_move,
    _staged_move,
)
from .sorter_types import (
    OrganizePlan,
    PlanOperation,
    SCHEMA_VERSION,
    SorterV2Error,
)


def _existing_plan_execution(
    plan: OrganizePlan,
    *,
    state_root: str | Path | None,
) -> dict[str, Any] | None:
    journals_dir = _state_category_root(state_root, "journals")
    if not journals_dir.is_dir():
        return None
    for path in journals_dir.glob("*.json"):
        try:
            value = _read_journal(path, state_root=state_root)
        except SorterV2Error:
            continue
        if not isinstance(value, dict) or value.get("plan_id") != plan.plan_id:
            continue
        status = str(value.get("status", ""))
        operations = value.get("operations", [])
        if status in {"committed", "completed_with_errors"}:
            errors = [str(item) for item in value.get("errors", [])]
            return {
                "ok": not errors,
                "type": "file-sorter-result",
                "schema_version": SCHEMA_VERSION,
                "plan_id": plan.plan_id,
                "transaction_id": str(value.get("transaction_id", "")),
                "journal_path": str(path),
                "target_dir": plan.target_dir,
                "moved_count": sum(
                    1
                    for item in operations
                    if item.get("status") == "committed"
                ),
                "unmatched_count": sum(
                    1 for item in plan.skipped if item.category == "unmatched"
                ),
                "skipped_count": len(plan.skipped),
                "errors": errors,
                "replayed": True,
            }
        raise SorterV2Error(
            f"Plan {plan.plan_id} already has transaction "
            f"{value.get('transaction_id')} in state {status}."
        )
    return None


def _target_lock_path(
    target_dir: str | Path,
    *,
    state_root: str | Path | None,
) -> Path:
    target = _validated_target_directory(target_dir)
    del state_root
    return target


def _execute_plan_operation(
    plan: OrganizePlan,
    target: Path,
    journal: _Journal,
    index: int,
    operation: PlanOperation,
) -> None:
    source, destination = _validate_operation_paths(target, operation)
    if not destination.parent.is_dir():
        raise SorterV2Error(
            f"Destination directory no longer exists: {destination.parent}"
        )
    if destination.exists():
        raise SorterV2Error(
            f"Destination appeared after preview: {destination}"
        )
    _ensure_source_unchanged(source, operation)
    stability = check_file_stability(
        source,
        quiet_seconds=plan.quiet_seconds,
    )
    if not stability.stable:
        raise SorterV2Error(
            f"Source is not stable ({stability.reason}): {source}"
        )
    journal.update_operation(index, status="running")
    if same_volume(source, destination.parent):
        digest = _same_volume_move(
            source,
            destination,
            operation,
            journal,
            index,
        )
    else:
        digest = _staged_move(
            source,
            destination,
            operation,
            journal,
            index,
        )
    journal.update_operation(index, sha256=digest)


def _record_operation_failure(
    journal: _Journal,
    index: int,
    source: Path,
    error: Exception,
    errors: list[str],
) -> None:
    message = f"{source.name}: {error}"
    errors.append(message)
    try:
        journal_operation = journal.value["operations"][index]
        stage_value = journal_operation.get("staging")
        if stage_value and journal_operation.get("status") in {
            "copying",
            "verified",
            "publishing",
            "published",
        }:
            _unlink_file_preserving_failure(
                Path(stage_value),
                missing_ok=True,
            )
        updates: dict[str, Any] = {"error": str(error)}
        if not journal_operation.get("publication_confirmed"):
            updates["status"] = "failed"
        journal.update_operation(index, **updates)
        journal.add_error(message)
    except OSError:
        pass


def _execution_result(
    plan: OrganizePlan,
    journal: _Journal,
    moved_count: int,
    errors: list[str],
) -> dict[str, Any]:
    return {
        "ok": not errors,
        "type": "file-sorter-result",
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "transaction_id": journal.transaction_id,
        "journal_path": str(journal.path),
        "target_dir": plan.target_dir,
        "moved_count": moved_count,
        "unmatched_count": sum(
            1 for item in plan.skipped if item.category == "unmatched"
        ),
        "skipped_count": len(plan.skipped),
        "errors": errors,
    }


def _execute_plan_under_lock(
    plan: OrganizePlan,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    target = _validated_target_directory(
        plan.target_dir,
        label="Plan target",
    )
    if _plan_expired(plan):
        raise SorterV2Error(
            f"Plan {plan.plan_id} expired; create a new preview."
        )
    existing = _existing_plan_execution(plan, state_root=state_root)
    if existing is not None:
        return existing
    journal = _Journal(plan, state_root=state_root)
    journal.set_transaction_status("in_progress")
    moved_count = 0
    errors: list[str] = []
    for index, operation in enumerate(plan.operations):
        source = Path(operation.source)
        destination = Path(operation.destination)
        del destination
        try:
            _execute_plan_operation(plan, target, journal, index, operation)
            moved_count += 1
        except (OSError, SorterV2Error, TypeError, ValueError) as error:
            _record_operation_failure(journal, index, source, error, errors)
    journal.set_transaction_status(
        "committed" if not errors else "completed_with_errors"
    )
    return _execution_result(plan, journal, moved_count, errors)


def execute_plan(
    plan: OrganizePlan,
    *,
    state_root: str | Path | None = None,
    lock_timeout_seconds: float = 5.0,
    pre_execute_validate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Execute a plan while holding the target-wide repository lock."""

    with _TargetDirectoryLock(
        plan.target_dir,
        timeout_seconds=max(0.0, lock_timeout_seconds),
    ):
        if pre_execute_validate is not None:
            pre_execute_validate()
        return _execute_plan_under_lock(plan, state_root=state_root)
