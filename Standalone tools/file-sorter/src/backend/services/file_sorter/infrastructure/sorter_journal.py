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




from .sorter_locks import (
    _atomic_write_json,
)
from .sorter_paths import (
    _same_path_identity,
    _state_category_root,
    _validated_state_document_path,
    _validated_target_directory,
    resolve_state_root,
)
from .sorter_types import (
    OrganizePlan,
    SCHEMA_VERSION,
    SorterV2Error,
    _utc_now,
    _validated_id,
)


class _Journal:
    def __init__(
        self,
        plan: OrganizePlan,
        *,
        state_root: str | Path | None = None,
    ) -> None:
        self.transaction_id = str(uuid.uuid4())
        self.path = (
            resolve_state_root(state_root)
            / "journals"
            / f"{self.transaction_id}.json"
        )
        _validated_state_document_path(
            self.path,
            state_root=state_root,
            category="journals",
            relative_parts=1,
            require_exists=False,
        )
        self.value: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "plan_id": plan.plan_id,
            "profile_id": plan.profile_id,
            "target_dir": plan.target_dir,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "status": "planned",
            "operations": [
                {
                    **item.to_dict(),
                    "status": "planned",
                    "staging": None,
                    "sha256": None,
                    "error": None,
                }
                for item in plan.operations
            ],
            "errors": [],
        }
        self.flush()

    def flush(self) -> None:
        self.value["updated_at"] = _utc_now()
        _atomic_write_json(self.path, self.value)

    def set_transaction_status(self, status: str) -> None:
        self.value["status"] = status
        self.flush()

    def update_operation(self, index: int, **updates: Any) -> None:
        self.value["operations"][index].update(updates)
        self.flush()

    def add_error(self, message: str) -> None:
        self.value["errors"].append(message)
        self.flush()


def _read_journal(
    path: Path,
    *,
    state_root: str | Path | None,
) -> dict[str, Any]:
    path = _validated_state_document_path(
        path,
        state_root=state_root,
        category="journals",
        relative_parts=1,
        require_exists=True,
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SorterV2Error(f"Cannot read journal {path}: {error}") from error
    if not isinstance(value, dict):
        raise SorterV2Error(f"Invalid journal document: {path}")
    if str(value.get("transaction_id", "")).strip() != path.stem:
        raise SorterV2Error(
            f"Journal identity does not match its state path: {path}"
        )
    target_text = str(value.get("target_dir", "")).strip()
    target = Path(target_text).expanduser()
    if not target_text or not target.is_absolute():
        raise SorterV2Error(f"Journal target is missing or invalid: {path}")
    if not _same_path_identity(target, target.resolve(strict=False)):
        raise SorterV2Error(f"Journal target is not canonical: {path}")
    return value


def transaction_history(
    *,
    state_root: str | Path | None = None,
    target_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    journals_dir = _state_category_root(state_root, "journals")
    if not journals_dir.is_dir():
        return []
    target = (
        str(_validated_target_directory(target_dir))
        if target_dir is not None
        else None
    )
    history: list[dict[str, Any]] = []
    for path in journals_dir.glob("*.json"):
        try:
            value = _read_journal(path, state_root=state_root)
        except SorterV2Error:
            continue
        if target is not None and value.get("target_dir") != target:
            continue
        operations = value.get("operations", [])
        history.append(
            {
                "transaction_id": value.get("transaction_id"),
                "plan_id": value.get("plan_id"),
                "profile_id": value.get("profile_id"),
                "target_dir": value.get("target_dir"),
                "status": value.get("status"),
                "created_at": value.get("created_at"),
                "updated_at": value.get("updated_at"),
                "moved_count": sum(
                    1
                    for item in operations
                    if item.get("status") in {"committed", "undone"}
                ),
                "error_count": len(value.get("errors", [])),
            }
        )
    return sorted(
        history,
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    )


def _journal_path(
    transaction_id: str,
    state_root: str | Path | None,
) -> Path:
    path = (
        resolve_state_root(state_root)
        / "journals"
        / f"{_validated_id(transaction_id, 'transaction id')}.json"
    )
    return _validated_state_document_path(
        path,
        state_root=state_root,
        category="journals",
        relative_parts=1,
        require_exists=False,
    )
