"""Trading audit — append-only JSONL journal.

This journal is the tool-local runtime mirror. The authoritative trading
audit records are written by ai-assistant (business owner) into its
canonical store; the engine forwards recordable events through the
governed channel when it is bound.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any


class TradingAudit:
    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / "trading-audit.jsonl"
        self._channel: Any | None = None

    def bind_channel(self, channel: Any | None) -> None:
        self._channel = channel

    @property
    def path(self) -> Path:
        return self._path

    def record(self, event_type: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
        event = {
            "event_id": f"evt-{uuid.uuid4().hex[:12]}",
            "type": str(event_type),
            "at": time.time(),
            "detail": dict(detail or {}),
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
        return event

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        lines = self._path.read_text(encoding="utf-8").splitlines()
        events: list[dict[str, Any]] = []
        for line in lines[-max(1, int(limit)):]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events
