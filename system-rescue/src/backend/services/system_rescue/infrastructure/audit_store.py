from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RescueRecordStore:
    """Write flat audit and log records inside the rescue-owned data root."""

    def __init__(self, data_root: Path) -> None:
        self.audit_path = data_root / "audit" / "history.jsonl"
        self.log_path = data_root / "logs" / "runtime.jsonl"

    @staticmethod
    def _timestamp() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _append(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def audit(self, action: str, *, ok: bool, **details: Any) -> None:
        self._append(
            self.audit_path,
            {
                "operation_id": uuid.uuid4().hex,
                "timestamp": self._timestamp(),
                "action": action,
                "ok": ok,
                **details,
            },
        )

    def log(self, event: str, **details: Any) -> None:
        self._append(
            self.log_path,
            {
                "timestamp": self._timestamp(),
                "event": event,
                **details,
            },
        )
