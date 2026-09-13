"""Durable sync journal shared by synchronization sub-sovereigns (A322).

Each synchronization child records every applied sync as an append-only,
bounded journal entry on the information-layer state surface and publishes
an A195 outbox audit event, so sync work is persistent, replayable and
auditable instead of living only in process memory.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SyncJournalError(RuntimeError):
    """Raised when a sync record cannot be durably recorded or audited.

    A121/A69 fail-closed: a sync that cannot be persisted or audited must
    surface as a failure — never as a silent success.
    """


class SyncJournal:
    """Bounded durable journal for one sub-sovereign's sync records."""

    def __init__(self, app: Any, sovereign_id: str, *, limit: int = 200) -> None:
        self._app = app
        self._sovereign_id = str(sovereign_id)
        self._limit = max(1, int(limit))
        root = getattr(app, "project_root", None) or Path.cwd()
        self._path = (
            Path(root)
            / "main-system"
            / "runtime"
            / "state"
            / "sync-journals"
            / f"{self._sovereign_id}.json"
        )
        self.records: list[dict[str, Any]] = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def _load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return []
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]

    def _persist(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(self.records, ensure_ascii=False, indent=1)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(payload + "\n", encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as error:
            raise SyncJournalError(
                f"sync journal persistence failed: {type(error).__name__}"
            ) from error

    def append(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Append one sync record durably and publish its audit event."""
        record = {
            "position": len(self.records) + 1,
            "kind": str(kind),
            "payload": payload,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }
        self.records.append(record)
        overflow = len(self.records) - self._limit
        if overflow > 0:
            del self.records[:overflow]
        self._persist()
        self._publish_audit(record)
        return record

    def _publish_audit(self, record: dict[str, Any]) -> None:
        """Publish the sync as an A195 outbox state event (audit chain).

        When a publisher is present, a failed publication raises so the sync
        is reported as refused rather than silently accepted (A121/A69).
        The durable journal entry itself remains the record of last resort
        for standalone/test contexts without a publisher.
        """
        publisher = getattr(self._app, "_outbox_publisher", None)
        append = getattr(publisher, "append_state_event", None)
        if not callable(append):
            return
        try:
            state = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
            append(
                entity_id=f"sync-journal:{self._sovereign_id}",
                entity_type="synchronization-journal",
                operation="upsert",
                changed_field_allowlist=("kind", "position"),
                invalidation_keys=("synchronization-journal", self._sovereign_id),
                state_hash=hashlib.sha256(state.encode("utf-8")).hexdigest(),
            )
        except Exception as error:
            raise SyncJournalError(
                f"sync audit publication failed: {type(error).__name__}"
            ) from error

    def latest(self) -> dict[str, Any] | None:
        return self.records[-1] if self.records else None

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.records[-max(1, int(limit)) :]


__all__ = ["SyncJournal", "SyncJournalError"]
