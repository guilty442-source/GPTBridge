"""Update history tracking with persistence — A181/A182/A183.

Tracks update history with file-based persistence.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core_system.update_manager_types import (
    UpdateManifest,
    UpdateStatus,
    UpdateType,
)


class UpdateHistory:
    """Tracks update history with persistence."""

    def __init__(self, project_root: Path, max_entries: int = 100) -> None:
        self.project_root = project_root
        self.max_entries = max_entries
        self._history_file = project_root / "main-system" / "runtime" / "state" / "update-history.json"
        self._history: deque[UpdateManifest] = deque(maxlen=max_entries)
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        """Load history from file."""
        try:
            if self._history_file.is_file():
                data = json.loads(self._history_file.read_text(encoding="utf-8"))
                with self._lock:
                    self._history.clear()
                    for entry in data.get("history", []):
                        entry["update_type"] = UpdateType(entry["update_type"])
                        entry["status"] = UpdateStatus(entry["status"])
                        self._history.append(UpdateManifest(**entry))
        except Exception:
            pass

    def _save(self) -> None:
        """Save history to file."""
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {
                    "version": 1,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "history": [entry.to_dict() for entry in self._history]
                }
                tmp = self._history_file.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8"
                )
                os.replace(tmp, self._history_file)
        except Exception:
            pass

    def add(self, manifest: UpdateManifest) -> None:
        """Add an update manifest to history."""
        with self._lock:
            self._history.append(manifest)
        self._save()

    def update(self, manifest: UpdateManifest) -> None:
        """Update an existing manifest in history."""
        with self._lock:
            for i, m in enumerate(self._history):
                if m.update_id == manifest.update_id:
                    self._history[i] = manifest
                    break
        self._save()

    def get_recent(self, count: int = 20) -> list[UpdateManifest]:
        """Get recent update history."""
        with self._lock:
            return list(self._history)[-count:]

    def get_by_id(self, update_id: str) -> Optional[UpdateManifest]:
        """Get manifest by update ID."""
        with self._lock:
            for m in self._history:
                if m.update_id == update_id:
                    return m
        return None

    def get_stats(self) -> dict:
        """Get update statistics."""
        with self._lock:
            total = len(self._history)
            if total == 0:
                return {"total": 0, "success_rate": 0.0, "avg_duration_ms": 0}
            successful = sum(1 for m in self._history if m.status == UpdateStatus.COMPLETED)
            failed = sum(1 for m in self._history if m.status == UpdateStatus.FAILED)
            rolled_back = sum(1 for m in self._history if m.status == UpdateStatus.ROLLED_BACK)
            durations = [m.duration_ms for m in self._history if m.duration_ms > 0]
            avg_duration = sum(durations) / len(durations) if durations else 0
            return {
                "total": total,
                "successful": successful,
                "failed": failed,
                "rolled_back": rolled_back,
                "success_rate": successful / total if total > 0 else 0.0,
                "avg_duration_ms": avg_duration,
            }


__all__ = ["UpdateHistory"]
