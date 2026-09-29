"""Registry-migration constants, helpers and records (A185 split).

Extracted from ``registry_migration_engine.py`` (source-size contract):
file-name constants, per-target field projections, the ``Classification``
crash taxonomy, JSON/atomic-write helpers and the ``StepRecord`` /
``TargetClassification`` / ``RecoveryReport`` / ``MigrationReport`` /
``_PreparedTarget`` record types.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .registry_migrations import TARGET_QUEUE_SCHEMA, TARGET_REGISTRY_SCHEMA

REGISTRY_FILE = "registry.json"
QUEUE_DIRECTORY = "merge-queue"
QUEUE_FILE = "queue.json"
LEDGER_FILE = "registry-migrations.jsonl"
JOURNAL_FILE = "registry-migration-journal.json"
AUTOMATION_STATE_SUBDIR = "gptbridge-automation"
TARGET_ORDER: tuple[str, ...] = ("registry", "queue")

_TARGET_SCHEMA: dict[str, int] = {
    "registry": TARGET_REGISTRY_SCHEMA,
    "queue": TARGET_QUEUE_SCHEMA,
}
_CHILD_FIELDS: tuple[str, ...] = (
    "branch",
    "worktree",
    "log",
    "pid",
    "restarts",
    "started_at",
)
_CHILD_DEFAULTS: dict[str, Any] = {
    "branch": "",
    "worktree": "",
    "log": "",
    "pid": 0,
    "restarts": 0,
    "started_at": 0.0,
}
_REGISTRY_TOP_FIELDS: tuple[str, ...] = (
    "pid",
    "started_at",
    "state",
    "sync_cycles",
    "push",
    "commit_dirty",
    "sync_interval",
    "health_interval",
    "last_sync",
    "last_sync_result",
    "health",
)
_QUEUE_ENTRY_FIELDS: tuple[str, ...] = (
    "queue_id",
    "enqueue_sequence",
    "worker_id",
    "task_id",
    "source_branch",
    "source_commit",
    "base_main_commit",
    "enqueue_time",
    "priority",
    "escalated_by",
    "attempt_count",
    "not_before",
    "blocked_reason",
    "status",
    "audit_id",
    "policy_hash",
    "updated_at",
)


class Classification(str, Enum):
    """Per-file crash classification."""

    OLD_VALID = "OLD_VALID"
    NEW_VALID = "NEW_VALID"
    PARTIAL = "PARTIAL"


class MigrationError(RuntimeError):
    """Raised when a migration cannot be prepared or validated."""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _json_text(payload: Any) -> str:
    return (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _atomic_write(path: Path, text: str) -> None:
    """temp -> fsync -> atomic replace; no partial file is ever visible."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _registry_view(payload: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {
        key: payload[key] for key in _REGISTRY_TOP_FIELDS if key in payload
    }
    view["children"] = [
        {
            key: child.get(key, _CHILD_DEFAULTS[key])
            for key in _CHILD_FIELDS
        }
        for child in payload.get("children", [])
        if isinstance(child, dict)
    ]
    return view


def _entry_view(entry: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {key: entry.get(key) for key in _QUEUE_ENTRY_FIELDS}
    view["state"] = entry.get("state", entry.get("status", ""))
    view["transaction_id"] = entry.get("transaction_id", "")
    return view


def _queue_view(payload: dict[str, Any]) -> dict[str, Any]:
    entries = payload.get("entries", [])
    return {
        "next_sequence": int(payload.get("next_sequence", 1)),
        "order": [entry.get("queue_id") for entry in entries],
        "sequence_order": [entry.get("enqueue_sequence") for entry in entries],
        "entries": [_entry_view(entry) for entry in entries],
    }


@dataclass
class StepRecord:
    """One applied migration step (ledger unit)."""

    migration_id: str
    target: str
    old_schema: int
    new_schema: int
    input_digest: str
    output_digest: str
    timestamp: str
    status: str = "APPLIED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "migration_id": self.migration_id,
            "target": self.target,
            "old_schema": self.old_schema,
            "new_schema": self.new_schema,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "timestamp": self.timestamp,
            "status": self.status,
        }


@dataclass(frozen=True)
class TargetClassification:
    target: str
    classification: Classification
    schema: Optional[int]
    digest: str
    reason: str = ""


@dataclass
class RecoveryReport:
    classifications: dict[str, str] = field(default_factory=dict)
    actions: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass
class MigrationReport:
    status: str
    dry_run: bool = False
    noop: bool = False
    classifications: dict[str, str] = field(default_factory=dict)
    steps: list[dict[str, Any]] = field(default_factory=list)
    backups: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recovery: Optional[RecoveryReport] = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.status in {"migrated", "noop", "dry-run"}


@dataclass
class _PreparedTarget:
    target: str
    path: Path
    source: dict[str, Any]
    migrated: dict[str, Any]
    records: list[StepRecord]
    source_schema: int
    target_schema: int
    source_generation: int
    generation: int
    backup_path: Optional[Path] = None
    backup_digest: str = ""


__all__ = [
    "AUTOMATION_STATE_SUBDIR", "Classification", "JOURNAL_FILE",
    "LEDGER_FILE", "MigrationError", "MigrationReport",
    "QUEUE_DIRECTORY", "QUEUE_FILE", "REGISTRY_FILE", "RecoveryReport",
    "StepRecord", "TargetClassification", "TARGET_ORDER",
    "_PreparedTarget", "_TARGET_SCHEMA", "_atomic_write",
    "_entry_view", "_is_int", "_is_number", "_json_text", "_now_iso",
    "_queue_view", "_registry_view",
]
