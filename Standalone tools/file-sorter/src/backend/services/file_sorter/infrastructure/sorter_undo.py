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
    _journal_path,
    _read_journal,
    transaction_history,
)
from .sorter_locks import (
    _TargetDirectoryLock,
    _atomic_write_json,
)
from .sorter_metadata import (
    _file_metadata_from_dict,
    _file_metadata_to_dict,
    _unlink_file_preserving_failure,
    _verify_file_metadata,
)
from .sorter_paths import (
    _state_category_root,
    _validate_journal_operation_paths,
    _validated_target_directory,
)
from .sorter_types import (
    SorterV2Error,
    TERMINAL_TRANSACTION_STATES,
    _FileMetadata,
    _utc_now,
    sha256_file,
)
from .sorter_undo_move import (
    _resume_undo_move,
)


def _undo_journal(
    path: Path,
    value: dict[str, Any],
) -> dict[str, Any]:
    transaction_id = str(value.get("transaction_id", path.stem))
    errors: list[str] = []
    undone_count = 0
    operations = value.get("operations", [])
    if not isinstance(operations, list):
        operations = []
        value["operations"] = operations
    has_pending_undo = any(
        isinstance(operation, dict)
        and operation.get("status") in {"committed", "undoing", "undo_failed"}
        for operation in operations
    )
    if has_pending_undo:
        value["status"] = "undo_in_progress"
        value["updated_at"] = _utc_now()
        _atomic_write_json(path, value)
    for operation in reversed(operations):
        if not isinstance(operation, dict):
            continue
        if operation.get("status") not in {
            "committed",
            "undoing",
            "undo_failed",
        }:
            continue
        moved_path = Path(str(operation.get("destination", "")))
        original_path = Path(str(operation.get("source", "")))
        expected_hash = str(operation.get("sha256") or "")
        try:
            original_path, moved_path, _stage = _validate_journal_operation_paths(
                value,
                operation,
            )
            if not expected_hash:
                raise SorterV2Error("Journal does not contain a SHA-256 digest.")
            previous_status = str(operation.get("status") or "")
            if previous_status == "committed":
                operation.update(
                    {
                        "undo_publication_confirmed": False,
                        "undo_publication_method": None,
                        "undo_publication_sha256": None,
                        "undo_publication_source": None,
                        "undo_publication_destination": None,
                        "undo_publication_metadata": None,
                    }
                )
            operation["status"] = "undoing"
            value["updated_at"] = _utc_now()
            _atomic_write_json(path, value)

            def persist_undo_publication(
                method: str,
                metadata: _FileMetadata,
            ) -> None:
                operation.update(
                    {
                        "undo_publication_confirmed": True,
                        "undo_publication_method": method,
                        "undo_publication_sha256": expected_hash,
                        "undo_publication_source": str(moved_path),
                        "undo_publication_destination": str(original_path),
                        "undo_publication_metadata": _file_metadata_to_dict(metadata),
                    }
                )
                value["updated_at"] = _utc_now()
                _atomic_write_json(path, value)

            _resume_undo_move(
                moved_path,
                original_path,
                expected_hash=expected_hash,
                operation=operation,
                on_published=persist_undo_publication,
            )
            operation["status"] = "undone"
            operation["undone_at"] = _utc_now()
            undone_count += 1
        except (OSError, SorterV2Error, TypeError, ValueError) as error:
            operation["status"] = "undo_failed"
            operation["undo_error"] = str(error)
            errors.append(f"{moved_path.name}: {error}")
        value["updated_at"] = _utc_now()
        _atomic_write_json(path, value)
    value["status"] = "undone" if not errors else "undo_failed"
    value["updated_at"] = _utc_now()
    if errors:
        value.setdefault("errors", []).extend(errors)
    _atomic_write_json(path, value)
    return {
        "ok": not errors,
        "type": "file-sorter-undo-result",
        "transaction_id": transaction_id,
        "undone_count": undone_count,
        "errors": errors,
    }


def _undo_transaction_under_lock(
    transaction_id: str,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    path = _journal_path(transaction_id, state_root)
    return _undo_journal(
        path,
        _read_journal(path, state_root=state_root),
    )


def _journal_has_interrupted_undo(value: Mapping[str, Any]) -> bool:
    if value.get("status") == "undo_in_progress":
        return True
    operations = value.get("operations", [])
    return isinstance(operations, list) and any(
        isinstance(operation, dict)
        and operation.get("status") == "undoing"
        for operation in operations
    )


def undo_transaction(
    transaction_id: str,
    *,
    state_root: str | Path | None = None,
    lock_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    path = _journal_path(transaction_id, state_root)
    value = _read_journal(path, state_root=state_root)
    target_dir = str(value.get("target_dir", "")).strip()
    if not target_dir:
        raise SorterV2Error(f"Journal has no target: {path}")
    with _TargetDirectoryLock(
        target_dir,
        timeout_seconds=max(0.0, lock_timeout_seconds),
    ):
        return _undo_transaction_under_lock(
            transaction_id,
            state_root=state_root,
        )


def undo_last_transaction(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    for item in transaction_history(state_root=state_root, target_dir=target_dir):
        if item.get("status") in {"committed", "completed_with_errors"}:
            return undo_transaction(
                str(item["transaction_id"]),
                state_root=state_root,
            )
    raise SorterV2Error(f"No committed transaction found for {target_dir}")


def recover_transactions(
    *,
    state_root: str | Path | None = None,
    target_dir: str | Path | None = None,
    lock_timeout_seconds: float = 5.0,
) -> list[dict[str, Any]]:
    """Recover interrupted publications and remove owned staging files.

    A source is deleted only when the journal durably confirms publication and
    both source and destination still match the recorded SHA-256 digest.
    """

    target = (
        str(_validated_target_directory(target_dir))
        if target_dir is not None
        else None
    )
    results: list[dict[str, Any]] = []
    journals_dir = _state_category_root(state_root, "journals")
    if not journals_dir.is_dir():
        return results
    for path in journals_dir.glob("*.json"):
        try:
            value = _read_journal(path, state_root=state_root)
        except SorterV2Error:
            continue
        if target is not None and value.get("target_dir") != target:
            continue
        if (
            value.get("status") in TERMINAL_TRANSACTION_STATES
            and not _journal_has_interrupted_undo(value)
        ):
            continue
        journal_target = str(value.get("target_dir", "")).strip()
        if not journal_target or not Path(journal_target).is_absolute():
            results.append(
                {
                    "transaction_id": value.get("transaction_id"),
                    "status": "recovery_failed",
                    "recovered_count": 0,
                    "errors": ["Journal target is missing or invalid."],
                }
            )
            continue
        try:
            with _TargetDirectoryLock(
                journal_target,
                timeout_seconds=max(0.0, lock_timeout_seconds),
            ):
                current = _read_journal(path, state_root=state_root)
                if (
                    current.get("status") in TERMINAL_TRANSACTION_STATES
                    and not _journal_has_interrupted_undo(current)
                ):
                    continue
                results.append(_recover_journal(path, current))
        except SorterV2Error as error:
            results.append(
                {
                    "transaction_id": value.get("transaction_id"),
                    "status": "busy",
                    "recovered_count": 0,
                    "errors": [str(error)],
                }
            )
    return results


def _recover_journal(
    path: Path,
    value: dict[str, Any],
) -> dict[str, Any]:
    if _journal_has_interrupted_undo(value):
        undo_result = _undo_journal(path, value)
        return {
            "transaction_id": undo_result["transaction_id"],
            "status": "undone" if undo_result["ok"] else "undo_failed",
            "recovered_count": undo_result["undone_count"],
            "errors": undo_result["errors"],
            "recovery_kind": "undo",
        }

    recovered = 0
    errors: list[str] = []
    operations = value.get("operations", [])
    if not isinstance(operations, list):
        operations = []
        value["operations"] = operations
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        source = Path(str(operation.get("source", "")))
        destination = Path(str(operation.get("destination", "")))
        status = str(operation.get("status", "planned"))
        digest = str(operation.get("sha256") or "")
        publication_proven = (
            status == "published"
            or operation.get("publication_confirmed") is True
        )
        try:
            source, destination, stage = _validate_journal_operation_paths(
                value,
                operation,
            )
            if stage is not None and stage.exists():
                _unlink_file_preserving_failure(stage)
            if publication_proven and digest:
                if (
                    destination.is_file()
                    and source.is_file()
                ):
                    if (
                        sha256_file(destination) == digest
                        and sha256_file(source) == digest
                    ):
                        expected_metadata = _file_metadata_from_dict(
                            operation.get("metadata")
                        )
                        if expected_metadata is not None:
                            _verify_file_metadata(
                                source,
                                expected_metadata,
                                label="Recovery source",
                            )
                            _verify_file_metadata(
                                destination,
                                expected_metadata,
                                label="Recovery destination",
                            )
                        source, destination, _ = (
                            _validate_journal_operation_paths(
                                value,
                                operation,
                            )
                        )
                        if (
                            operation.get("publication_method") == "hardlink"
                            and not os.path.samefile(source, destination)
                        ):
                            raise SorterV2Error(
                                "Recovery hard-link ownership proof failed."
                            )
                        _unlink_file_preserving_failure(source)
                        operation["status"] = "committed"
                        operation["staging"] = None
                        recovered += 1
                        continue
                elif (
                    destination.is_file()
                    and not source.exists()
                ):
                    if sha256_file(destination) == digest:
                        operation["status"] = "committed"
                        operation["staging"] = None
                        recovered += 1
                        continue
            if status not in {"committed", "undone"}:
                operation["status"] = "recovery_failed"
                retained_error = (
                    "Source retained; operation was not safely published."
                )
                operation["error"] = retained_error
                errors.append(f"{source.name}: {retained_error}")
        except (OSError, SorterV2Error, TypeError, ValueError) as error:
            operation["status"] = "recovery_failed"
            operation["error"] = str(error)
            errors.append(f"{source.name}: {error}")
    unfinished = [
        item
        for item in operations
        if isinstance(item, dict)
        and item.get("status") not in {"committed", "undone"}
    ]
    value["status"] = "committed" if not unfinished else "completed_with_errors"
    value["updated_at"] = _utc_now()
    if errors:
        value.setdefault("errors", []).extend(errors)
    _atomic_write_json(path, value)
    return {
        "transaction_id": value.get("transaction_id"),
        "status": value["status"],
        "recovered_count": recovered,
        "errors": errors,
    }
