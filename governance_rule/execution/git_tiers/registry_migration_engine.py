"""Crash-safe registry/merge-queue schema migration engine (task §322-324, §348-350).

Contract:

* dry-run   — registry/queue are copied into a temporary sandbox, migrated,
  validated and invariant-checked there; the real state is never touched.
* formal    — ``registry.json`` (and the merge queue) is copied to
  ``registry.backup.<generation>`` (verified by a digest sidecar), migrated,
  validated, checked against the source payload, then *atomically* replaced
  (temp -> fsync -> ``os.replace``).  A failure leaves the old file intact;
  no partial overwrite is ever written.
* recovery  — every file is classified OLD_VALID / NEW_VALID / PARTIAL.
  PARTIAL never adopts the new data: it is restored from a verified backup
  or quarantined (untouched) when no verified backup exists.
* queue     — migration preserves ``source_commit``, ``base_main_commit``,
  ``transaction_id``, ``state``, entry ordering and priority.  SHAs are
  never recomputed, entries are never re-sorted and a ``failed`` entry is
  never promoted to ``pending``.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from .git_repository import GitRepository
from .registry_migrations import (
    QUEUE_STEPS,
    REGISTRY_STEPS,
    STEPS_BY_TARGET,
    TARGET_QUEUE_SCHEMA,
    TARGET_REGISTRY_SCHEMA,
    MigrationStep,
    SchemaError,
    canonical_digest,
    deep_copy,
    detect_schema,
)

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


class RegistryMigrationEngine:
    """Migrate one ``.git/gptbridge-automation`` state directory safely."""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)
        self.queue_dir = self.state_dir / QUEUE_DIRECTORY
        self.registry_path = self.state_dir / REGISTRY_FILE
        self.queue_path = self.queue_dir / QUEUE_FILE
        self.ledger_path = self.state_dir / LEDGER_FILE
        self.journal_path = self.state_dir / JOURNAL_FILE

    @classmethod
    def for_root(cls, root: str | Path) -> "RegistryMigrationEngine":
        """Resolve the shared automation state directory for a worktree."""
        repo = GitRepository(root)
        result = repo.run(["rev-parse", "--git-common-dir"])
        raw = (result.stdout or "").strip()
        common = Path(raw)
        if not common.is_absolute():
            common = repo.path / common
        return cls(common.resolve() / AUTOMATION_STATE_SUBDIR)

    # -- paths ----------------------------------------------------------

    def path_for(self, target: str) -> Path:
        if target == "registry":
            return self.registry_path
        if target == "queue":
            return self.queue_path
        raise MigrationError(f"unknown migration target: {target}")

    # -- io -------------------------------------------------------------

    @staticmethod
    def _read_payload(
        path: Path,
    ) -> tuple[Optional[dict[str, Any]], str, str]:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as error:
            return None, "", f"unreadable: {error}"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            return None, "", f"invalid-json: {error}"
        if not isinstance(payload, dict):
            return None, "", "not-a-mapping"
        return payload, canonical_digest(payload), ""

    def _load_journal(self) -> dict[str, Any]:
        payload, _, _ = self._read_payload(self.journal_path)
        return payload or {}

    def _write_journal(self, journal: dict[str, Any]) -> None:
        _atomic_write(self.journal_path, _json_text(journal))

    def _append_ledger(self, records: list[StepRecord]) -> None:
        if not records:
            return
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path, "a", encoding="utf-8", newline="\n") as handle:
            for record in records:
                handle.write(
                    json.dumps(
                        record.to_dict(), ensure_ascii=False, sort_keys=True
                    )
                    + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())

    # -- classification -------------------------------------------------

    def classify(self) -> dict[str, TargetClassification]:
        journal = self._load_journal()
        result: dict[str, TargetClassification] = {}
        for target in TARGET_ORDER:
            path = self.path_for(target)
            if not path.is_file():
                continue
            result[target] = self._classify_one(target, journal)
        return result

    def _classify_one(
        self, target: str, journal: dict[str, Any]
    ) -> TargetClassification:
        path = self.path_for(target)
        payload, digest, error = self._read_payload(path)
        if payload is None:
            return TargetClassification(
                target, Classification.PARTIAL, None, "", error
            )
        try:
            schema: Optional[int] = detect_schema(payload)
        except SchemaError as exc:
            return TargetClassification(
                target, Classification.PARTIAL, None, digest, str(exc)
            )
        target_schema = _TARGET_SCHEMA[target]
        expected = (
            (journal.get("targets") or {}).get(target) or {}
            if journal
            else {}
        )
        if expected:
            if digest == expected.get("output_digest"):
                errors = self._validate(target, payload)
                errors.extend(self._invariant_from_backup(target, payload))
                if not errors:
                    return TargetClassification(
                        target,
                        Classification.NEW_VALID,
                        schema,
                        digest,
                        "matches recorded migration output",
                    )
                return TargetClassification(
                    target,
                    Classification.PARTIAL,
                    schema,
                    digest,
                    "; ".join(errors),
                )
            if digest == expected.get("input_digest"):
                return TargetClassification(
                    target,
                    Classification.OLD_VALID,
                    schema,
                    digest,
                    "migration not applied (recorded input)",
                )
            return TargetClassification(
                target,
                Classification.PARTIAL,
                schema,
                digest,
                "digest matches neither recorded input nor output",
            )
        if schema == target_schema:
            errors = self._validate(target, payload)
            if not errors:
                return TargetClassification(
                    target, Classification.NEW_VALID, schema, digest, ""
                )
            return TargetClassification(
                target,
                Classification.PARTIAL,
                schema,
                digest,
                "; ".join(errors),
            )
        first_schema = STEPS_BY_TARGET[target][0].old_schema
        if schema < first_schema:
            return TargetClassification(
                target,
                Classification.PARTIAL,
                schema,
                digest,
                f"schema {schema} below migration chain",
            )
        if schema < target_schema and self._shape_ok(target, payload):
            return TargetClassification(
                target,
                Classification.OLD_VALID,
                schema,
                digest,
                "legacy schema, migration pending",
            )
        if schema > target_schema:
            return TargetClassification(
                target,
                Classification.PARTIAL,
                schema,
                digest,
                "schema newer than engine target; refusing to downgrade",
            )
        return TargetClassification(
            target,
            Classification.PARTIAL,
            schema,
            digest,
            "legacy schema fails shape check",
        )

    def _invariant_from_backup(
        self, target: str, payload: dict[str, Any]
    ) -> list[str]:
        backup = self._verified_backup(target, self._load_journal())
        if backup is None:
            return []
        source = backup[0]
        return self._invariant(target, source, payload)

    # -- validation -----------------------------------------------------

    def _validate(self, target: str, payload: dict[str, Any]) -> list[str]:
        if target == "registry":
            return self._validate_registry(payload)
        return self._validate_queue(payload)

    @staticmethod
    def _validate_registry(payload: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        try:
            schema = detect_schema(payload, default=0)
        except SchemaError as exc:
            return [str(exc)]
        if schema != TARGET_REGISTRY_SCHEMA:
            errors.append(
                f"registry schema {schema} != {TARGET_REGISTRY_SCHEMA}"
            )
        if "generation" in payload and not _is_int(payload["generation"]):
            errors.append("registry generation must be an integer")
        for key in ("pid", "sync_cycles"):
            if key in payload and not _is_int(payload[key]):
                errors.append(f"registry {key} must be an integer")
        for key in ("started_at", "sync_interval", "health_interval"):
            if key in payload and not _is_number(payload[key]):
                errors.append(f"registry {key} must be a number")
        if "health" in payload and not isinstance(payload["health"], dict):
            errors.append("registry health must be a mapping")
        children = payload.get("children")
        if not isinstance(children, list):
            errors.append("registry children must be a list")
            return errors
        for index, child in enumerate(children):
            if not isinstance(child, dict):
                errors.append(f"registry child {index} must be a mapping")
                continue
            for key in ("pid", "restarts"):
                if key in child and not _is_int(child[key]):
                    errors.append(f"registry child {index} {key} must be integer")
            if "started_at" in child and not _is_number(child["started_at"]):
                errors.append(f"registry child {index} started_at must be number")
            if "watcher_id" in child and not isinstance(child["watcher_id"], str):
                errors.append(f"registry child {index} watcher_id must be string")
            if "state" in child and not isinstance(child["state"], str):
                errors.append(f"registry child {index} state must be string")
        return errors

    @staticmethod
    def _validate_queue(payload: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        try:
            schema = detect_schema(payload, default=0)
        except SchemaError as exc:
            return [str(exc)]
        if schema != TARGET_QUEUE_SCHEMA:
            errors.append(f"queue schema {schema} != {TARGET_QUEUE_SCHEMA}")
        if "generation" in payload and not _is_int(payload["generation"]):
            errors.append("queue generation must be an integer")
        next_sequence = payload.get("next_sequence")
        if not _is_int(next_sequence) or int(next_sequence) < 1:
            errors.append("queue next_sequence must be an integer >= 1")
        entries = payload.get("entries")
        if not isinstance(entries, list):
            errors.append("queue entries must be a list")
            return errors
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                errors.append(f"queue entry {index} must be a mapping")
                continue
            for key in ("source_commit", "base_main_commit", "status"):
                value = entry.get(key)
                if not isinstance(value, str) or not value:
                    errors.append(f"queue entry {index} {key} must be a string")
            if "priority" in entry and not _is_int(entry["priority"]):
                errors.append(f"queue entry {index} priority must be integer")
            if "enqueue_sequence" in entry and not _is_int(
                entry["enqueue_sequence"]
            ):
                errors.append(
                    f"queue entry {index} enqueue_sequence must be integer"
                )
            if "state" in entry and not isinstance(entry["state"], str):
                errors.append(f"queue entry {index} state must be string")
            if "transaction_id" in entry and not isinstance(
                entry["transaction_id"], str
            ):
                errors.append(
                    f"queue entry {index} transaction_id must be string"
                )
        return errors

    @staticmethod
    def _shape_ok(target: str, payload: dict[str, Any]) -> bool:
        if target == "registry":
            children = payload.get("children", [])
            return isinstance(children, list) and all(
                isinstance(child, dict) for child in children
            )
        entries = payload.get("entries", [])
        return isinstance(entries, list) and all(
            isinstance(entry, dict) for entry in entries
        )

    # -- invariants -----------------------------------------------------

    def _invariant(
        self,
        target: str,
        source: dict[str, Any],
        migrated: dict[str, Any],
    ) -> list[str]:
        if target == "registry":
            return self._invariant_registry(source, migrated)
        return self._invariant_queue(source, migrated)

    @staticmethod
    def _invariant_registry(
        source: dict[str, Any], migrated: dict[str, Any]
    ) -> list[str]:
        errors: list[str] = []
        if _registry_view(source) != _registry_view(migrated):
            errors.append(
                "registry content changed outside the migration contract"
            )
        if len(source.get("children", [])) != len(migrated.get("children", [])):
            errors.append("registry children count changed")
        missing = set(source) - set(migrated)
        if missing:
            errors.append(f"registry lost keys: {sorted(missing)}")
        for index, (before, after) in enumerate(
            zip(source.get("children", []), migrated.get("children", []))
        ):
            if isinstance(before, dict) and isinstance(after, dict):
                lost = set(before) - set(after)
                if lost:
                    errors.append(
                        f"registry child {index} lost keys: {sorted(lost)}"
                    )
        return errors

    @staticmethod
    def _invariant_queue(
        source: dict[str, Any], migrated: dict[str, Any]
    ) -> list[str]:
        errors: list[str] = []
        if _queue_view(source) != _queue_view(migrated):
            errors.append(
                "queue content changed outside the migration contract "
                "(ordering/priority/SHA/state must be preserved)"
            )
        source_entries = source.get("entries", [])
        migrated_entries = migrated.get("entries", [])
        if len(source_entries) != len(migrated_entries):
            errors.append("queue entry count changed")
        missing = set(source) - set(migrated)
        if missing:
            errors.append(f"queue lost keys: {sorted(missing)}")
        for before, after in zip(source_entries, migrated_entries):
            if isinstance(before, dict) and isinstance(after, dict):
                lost = set(before) - set(after)
                if lost:
                    errors.append(
                        f"queue entry {after.get('queue_id')} lost keys: "
                        f"{sorted(lost)}"
                    )
            for key in ("source_commit", "base_main_commit"):
                if before.get(key) != after.get(key):
                    errors.append(
                        f"queue entry {after.get('queue_id')}: {key} changed"
                    )
            if before.get("status") != after.get("status"):
                errors.append(
                    f"queue entry {after.get('queue_id')}: status changed"
                )
            if after.get("status") == "failed" and after.get("state") not in {
                "failed",
                "",
            }:
                errors.append(
                    f"queue entry {after.get('queue_id')}: failed promoted"
                )
        return errors

    # -- preparation ----------------------------------------------------

    def _apply_steps(
        self, target: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], list[StepRecord], int, int]:
        steps: tuple[MigrationStep, ...] = STEPS_BY_TARGET[target]
        target_schema = _TARGET_SCHEMA[target]
        data = deep_copy(payload)
        schema = detect_schema(data, default=steps[0].old_schema)
        if schema > target_schema:
            raise SchemaError(
                f"{target} schema {schema} newer than target {target_schema}"
            )
        records: list[StepRecord] = []
        while schema < target_schema:
            step = next(
                (candidate for candidate in steps if candidate.old_schema == schema),
                None,
            )
            if step is None:
                raise SchemaError(
                    f"no {target} migration step from schema {schema}"
                )
            input_digest = canonical_digest(data)
            data = step.apply(data)
            new_schema = detect_schema(data, default=step.new_schema)
            if new_schema <= schema:
                raise SchemaError(
                    f"{step.migration_id} did not advance schema"
                )
            records.append(
                StepRecord(
                    migration_id=step.migration_id,
                    target=target,
                    old_schema=schema,
                    new_schema=new_schema,
                    input_digest=input_digest,
                    output_digest=canonical_digest(data),
                    timestamp=_now_iso(),
                )
            )
            schema = new_schema
        source_generation = int(payload.get("generation", 0) or 0)
        generation = source_generation + 1
        data["generation"] = generation
        if records:
            records[-1].output_digest = canonical_digest(data)
        return data, records, source_generation, generation

    def _prepare(self, targets: list[str]) -> list[_PreparedTarget]:
        prepared: list[_PreparedTarget] = []
        for target in targets:
            path = self.path_for(target)
            source, _, error = self._read_payload(path)
            if source is None:
                raise MigrationError(f"{target} state is unreadable: {error}")
            migrated, records, source_generation, generation = (
                self._apply_steps(target, source)
            )
            if not records:
                continue
            validation = self._validate(target, migrated)
            if validation:
                raise MigrationError(
                    f"{target} migration failed validation: "
                    + "; ".join(validation)
                )
            invariants = self._invariant(target, source, migrated)
            if invariants:
                raise MigrationError(
                    f"{target} migration violates invariants: "
                    + "; ".join(invariants)
                )
            prepared.append(
                _PreparedTarget(
                    target=target,
                    path=path,
                    source=source,
                    migrated=migrated,
                    records=records,
                    source_schema=detect_schema(
                        source,
                        default=STEPS_BY_TARGET[target][0].old_schema,
                    ),
                    target_schema=_TARGET_SCHEMA[target],
                    source_generation=source_generation,
                    generation=generation,
                )
            )
        return prepared

    # -- backups --------------------------------------------------------

    def _backup_base(self, target: str, generation: int) -> Path:
        if target == "registry":
            return self.state_dir / f"registry.backup.{generation}"
        return self.queue_dir / f"queue.backup.{generation}"

    def _backup_path(self, target: str, generation: int) -> Path:
        base = self._backup_base(target, generation)
        candidate = base
        counter = 1
        while candidate.exists() or candidate.with_name(
            candidate.name + ".meta.json"
        ).exists():
            candidate = base.with_name(f"{base.name}.{counter}")
            counter += 1
        return candidate

    def _write_backup(self, item: _PreparedTarget) -> None:
        backup_path = self._backup_path(item.target, item.source_generation)
        _atomic_write(backup_path, _json_text(item.source))
        meta = {
            "backup_file": backup_path.name,
            "backup_digest": canonical_digest(item.source),
            "source_schema": item.source_schema,
            "source_generation": item.source_generation,
            "target_schema": item.target_schema,
            "migration_ids": [record.migration_id for record in item.records],
            "created_at": _now_iso(),
        }
        _atomic_write(
            backup_path.with_name(backup_path.name + ".meta.json"),
            _json_text(meta),
        )
        item.backup_path = backup_path
        item.backup_digest = canonical_digest(item.source)

    def _backup_candidates(self, target: str) -> list[Path]:
        directory = self.state_dir if target == "registry" else self.queue_dir
        if not directory.is_dir():
            return []
        prefix = "registry.backup." if target == "registry" else "queue.backup."
        return [
            path
            for path in directory.glob(f"{prefix}*")
            if path.is_file() and not path.name.endswith(".meta.json")
        ]

    def _verified_backup(
        self, target: str, journal: dict[str, Any]
    ) -> Optional[tuple[dict[str, Any], Path, dict[str, Any]]]:
        expected = ((journal.get("targets") or {}).get(target) or {}).get(
            "input_digest"
        )
        candidates = sorted(
            self._backup_candidates(target),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in candidates:
            payload, digest, error = self._read_payload(path)
            if payload is None:
                continue
            meta, _, _ = self._read_payload(
                path.with_name(path.name + ".meta.json")
            )
            if meta is None or meta.get("backup_digest") != digest:
                continue
            if expected and digest != expected:
                continue
            return payload, path, meta
        return None

    # -- journal --------------------------------------------------------

    def _build_journal(self, prepared: list[_PreparedTarget]) -> dict[str, Any]:
        return {
            "phase": "PREPARED",
            "created_at": _now_iso(),
            "state_dir": str(self.state_dir),
            "targets": {
                item.target: {
                    "path": str(item.path),
                    "source_schema": item.source_schema,
                    "target_schema": item.target_schema,
                    "input_digest": canonical_digest(item.source),
                    "output_digest": canonical_digest(item.migrated),
                    "source_generation": item.source_generation,
                    "generation": item.generation,
                    "backup": str(item.backup_path) if item.backup_path else "",
                    "backup_digest": item.backup_digest,
                }
                for item in prepared
            },
            "steps": [
                record.to_dict()
                for item in prepared
                for record in item.records
            ],
        }

    # -- public api -----------------------------------------------------

    def dry_run(self) -> MigrationReport:
        """Copy state to a sandbox, migrate/validate/check invariants there."""
        with tempfile.TemporaryDirectory(
            prefix="gbt-registry-migration-"
        ) as tmp:
            sandbox = Path(tmp) / AUTOMATION_STATE_SUBDIR
            for target in TARGET_ORDER:
                source = self.path_for(target)
                if not source.is_file():
                    continue
                destination = sandbox / source.relative_to(self.state_dir)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(source.read_bytes())
            sandbox_engine = RegistryMigrationEngine(sandbox)
            report = sandbox_engine._execute()
            report.dry_run = True
            if report.ok:
                report.status = "dry-run"
            return report

    def migrate(self) -> MigrationReport:
        """Formal run: backup -> migrate -> validate -> atomic replace."""
        return self._execute()

    def recover(self) -> RecoveryReport:
        """Classify every state file; restore PARTIAL from a verified backup."""
        classifications = self.classify()
        journal = self._load_journal()
        actions: dict[str, str] = {}
        errors: list[str] = []
        for target, info in classifications.items():
            if info.classification is Classification.OLD_VALID:
                actions[target] = "KEEP_OLD"
                continue
            if info.classification is Classification.NEW_VALID:
                actions[target] = "KEEP_NEW"
                continue
            backup = self._verified_backup(target, journal)
            if backup is None:
                actions[target] = "QUARANTINED"
                errors.append(
                    f"{target}: PARTIAL without verified backup "
                    f"({info.reason}); new data not adopted"
                )
                continue
            payload, backup_path, _ = backup
            _atomic_write(self.path_for(target), _json_text(payload))
            actions[target] = "RESTORED"
            self._append_ledger(
                [
                    StepRecord(
                        migration_id=f"recovery-restore-{target}",
                        target=target,
                        old_schema=info.schema or 0,
                        new_schema=detect_schema(payload),
                        input_digest=info.digest,
                        output_digest=canonical_digest(payload),
                        timestamp=_now_iso(),
                        status="RESTORED",
                    )
                ]
            )
            journal.setdefault("recovery", {})[target] = {
                "action": "RESTORED",
                "backup": str(backup_path),
                "restored_at": _now_iso(),
            }
        self._cleanup_temp_files()
        if journal.get("recovery"):
            try:
                self._write_journal(journal)
            except OSError:
                pass
        return RecoveryReport(
            classifications={
                target: info.classification.value
                for target, info in classifications.items()
            },
            actions=actions,
            errors=errors,
        )

    def _cleanup_temp_files(self) -> None:
        for path in (self.registry_path, self.queue_path):
            if not path.parent.is_dir():
                continue
            for tmp in path.parent.glob(f"{path.name}.*.tmp"):
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # -- execution ------------------------------------------------------

    def _execute(self) -> MigrationReport:
        classifications = self.classify()
        flat = {
            target: info.classification.value
            for target, info in classifications.items()
        }
        if not classifications:
            return MigrationReport(
                status="blocked",
                errors=["no registry/queue state files found"],
            )
        if "registry" not in classifications:
            return MigrationReport(
                status="blocked",
                classifications=flat,
                errors=["registry.json is missing"],
            )
        partial = {
            target: info
            for target, info in classifications.items()
            if info.classification is Classification.PARTIAL
        }
        if partial:
            return MigrationReport(
                status="blocked",
                classifications=flat,
                errors=[
                    f"{target}: PARTIAL ({info.reason}); "
                    "run recover() before migrating"
                    for target, info in partial.items()
                ],
            )
        pending = [
            target
            for target in TARGET_ORDER
            if target in classifications
            and classifications[target].classification is Classification.OLD_VALID
        ]
        if not pending:
            return MigrationReport(
                status="noop", noop=True, classifications=flat
            )
        try:
            prepared = self._prepare(pending)
        except (MigrationError, SchemaError) as error:
            return MigrationReport(
                status="failed",
                classifications=flat,
                errors=[str(error)],
            )
        if not prepared:
            return MigrationReport(
                status="noop", noop=True, classifications=flat
            )
        try:
            for item in prepared:
                self._write_backup(item)
            journal = self._build_journal(prepared)
            self._write_journal(journal)
        except OSError as error:
            return MigrationReport(
                status="failed",
                classifications=flat,
                errors=[f"backup/journal write failed: {error}"],
            )
        applied: list[dict[str, Any]] = []
        try:
            for item in prepared:
                _atomic_write(item.path, _json_text(item.migrated))
                self._append_ledger(item.records)
                applied.extend(record.to_dict() for record in item.records)
        except OSError as error:
            journal["phase"] = "FAILED"
            journal["error"] = str(error)
            journal["failed_at"] = _now_iso()
            try:
                self._write_journal(journal)
            except OSError:
                pass
            return MigrationReport(
                status="failed",
                classifications=flat,
                steps=applied,
                backups={
                    item.target: str(item.backup_path)
                    for item in prepared
                    if item.backup_path
                },
                errors=[f"atomic replace failed; old state preserved: {error}"],
            )
        warnings: list[str] = []
        journal["phase"] = "COMPLETED"
        journal["completed_at"] = _now_iso()
        try:
            self._write_journal(journal)
        except OSError as error:
            warnings.append(f"journal finalize failed: {error}")
        return MigrationReport(
            status="migrated",
            classifications=flat,
            steps=applied,
            backups={
                item.target: str(item.backup_path)
                for item in prepared
                if item.backup_path
            },
            warnings=warnings,
        )


__all__ = [
    "AUTOMATION_STATE_SUBDIR",
    "Classification",
    "JOURNAL_FILE",
    "LEDGER_FILE",
    "MigrationError",
    "MigrationReport",
    "QUEUE_FILE",
    "RecoveryReport",
    "REGISTRY_FILE",
    "RegistryMigrationEngine",
    "StepRecord",
    "TargetClassification",
    "TARGET_ORDER",
]
