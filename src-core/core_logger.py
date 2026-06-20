from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_LOG_FILE_BYTES = 5 * 1024 * 1024
MAX_LOG_BACKUPS = 3
MAX_PAYLOAD_DEPTH = 4
MAX_PAYLOAD_ITEMS = 50
MAX_PAYLOAD_TEXT_CHARS = 4000


class CoreLogger:
    """Simple structured logger for backend runtime events."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.logs_root = self.project_root / "runtime" / "logs"
        self.logs_root.mkdir(parents=True, exist_ok=True)

    def write(self, category: str, message: str, payload: Any | None = None) -> Path:
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "category": category,
            "message": message,
            "payload": self._compact_value(payload or {}),
        }

        file_name = "core.log"
        if category == "error":
            file_name = "error.log"
        elif category:
            file_name = f"{category}.log"

        target = self.logs_root / file_name
        self._rotate_if_needed(target)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return target

    def _rotate_if_needed(self, target: Path) -> None:
        try:
            if not target.exists() or target.stat().st_size < MAX_LOG_FILE_BYTES:
                return

            oldest = target.with_name(f"{target.name}.{MAX_LOG_BACKUPS}")
            if oldest.exists():
                oldest.unlink()

            for index in range(MAX_LOG_BACKUPS - 1, 0, -1):
                current = target.with_name(f"{target.name}.{index}")
                if current.exists():
                    current.replace(target.with_name(f"{target.name}.{index + 1}"))

            target.replace(target.with_name(f"{target.name}.1"))
        except OSError:
            return

    def _compact_value(self, value: Any, depth: int = 0) -> Any:
        if depth >= MAX_PAYLOAD_DEPTH:
            return self._compact_scalar(value)

        if isinstance(value, dict):
            compacted: dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= MAX_PAYLOAD_ITEMS:
                    compacted["..."] = f"truncated {len(value) - MAX_PAYLOAD_ITEMS} entries"
                    break
                compacted[str(key)] = self._compact_value(item, depth + 1)
            return compacted

        if isinstance(value, (list, tuple, set)):
            values = list(value)
            compacted_list = [
                self._compact_value(item, depth + 1)
                for item in values[:MAX_PAYLOAD_ITEMS]
            ]
            if len(values) > MAX_PAYLOAD_ITEMS:
                compacted_list.append(f"... truncated {len(values) - MAX_PAYLOAD_ITEMS} items")
            return compacted_list

        return self._compact_scalar(value)

    @staticmethod
    def _compact_scalar(value: Any) -> Any:
        if isinstance(value, (str, bytes, bytearray)):
            text = value.decode("utf-8", errors="replace") if not isinstance(value, str) else value
            if len(text) > MAX_PAYLOAD_TEXT_CHARS:
                omitted = len(text) - MAX_PAYLOAD_TEXT_CHARS
                return f"{text[:MAX_PAYLOAD_TEXT_CHARS]}... [truncated {omitted} chars]"
            return text

        if isinstance(value, (int, float, bool)) or value is None:
            return value

        text = str(value)
        if len(text) > MAX_PAYLOAD_TEXT_CHARS:
            omitted = len(text) - MAX_PAYLOAD_TEXT_CHARS
            return f"{text[:MAX_PAYLOAD_TEXT_CHARS]}... [truncated {omitted} chars]"
        return text

    # Logging-API compatible helpers for callers that expect stdlib logger methods.
    def debug(self, message: str, payload: Any | None = None) -> Path:
        return self.write("debug", message, payload)

    def info(self, message: str, payload: Any | None = None) -> Path:
        return self.write("info", message, payload)

    def warning(self, message: str, payload: Any | None = None) -> Path:
        return self.write("warning", message, payload)

    def error(self, message: str, payload: Any | None = None) -> Path:
        return self.write("error", message, payload)
