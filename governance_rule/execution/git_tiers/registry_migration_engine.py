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

Module layout (A185 source-size split):

    registry_migration_support.py  constants + helpers + record types
    registry_migration_checks.py   MigrationChecksMixin — classify/validate/
                                   invariants
    registry_migration_ops.py      MigrationOpsMixin — prepare/backup/
                                   journal/public api/execute
    registry_migration_engine.py   RegistryMigrationEngine seam (this module)
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

from .git_repository import GitRepository
from .registry_migrations import canonical_digest
from .registry_migration_checks import MigrationChecksMixin
from .registry_migration_ops import MigrationOpsMixin
from .registry_migration_support import (  # noqa: F401  (re-exported surface)
    AUTOMATION_STATE_SUBDIR,
    JOURNAL_FILE,
    LEDGER_FILE,
    QUEUE_DIRECTORY,
    QUEUE_FILE,
    REGISTRY_FILE,
    TARGET_ORDER,
    Classification,
    MigrationError,
    MigrationReport,
    RecoveryReport,
    StepRecord,
    TargetClassification,
    _PreparedTarget,
    _atomic_write,
    _entry_view,
    _is_int,
    _is_number,
    _json_text,
    _now_iso,
    _queue_view,
    _registry_view,
)


class RegistryMigrationEngine(MigrationChecksMixin, MigrationOpsMixin):
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
