from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from utils.cleanup import quarantine_path
from core_system.storage_paths import log_root

MAX_LOG_FILE_BYTES = 5 * 1024 * 1024
MAX_LOG_BACKUPS = 1
MAX_PAYLOAD_DEPTH = 4
MAX_PAYLOAD_ITEMS = 50
MAX_PAYLOAD_TEXT_CHARS = 4000
MAX_LOG_RECORD_BYTES = 64 * 1024
LOG_DEDUP_WINDOW_SECONDS = 30.0
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|[_-])(?:authorization|cookie|credential|password|secret|session|token|api[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)


class CoreLogger:
    """Simple structured logger for backend runtime events."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.logs_root = log_root(self.project_root) / "main-system"
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._dedupe: dict[str, tuple[float, int]] = {}

    def write(self, category: str, message: str, payload: Any | None = None) -> Path:
        file_name = "core.log"
        if category == "error":
            file_name = "error.log"
        elif category:
            file_name = f"{category}.log"
        target = self.logs_root / file_name
        compacted_payload = self._compact_value(payload or {})
        fingerprint = self._dedupe_fingerprint(category, message, compacted_payload)
        now = time.monotonic()
        with self._write_lock:
            previous_at, suppressed = self._dedupe.get(fingerprint, (0.0, 0))
            if now - previous_at < LOG_DEDUP_WINDOW_SECONDS:
                self._dedupe[fingerprint] = (previous_at, suppressed + 1)
                return target
            self._dedupe[fingerprint] = (now, 0)
            if suppressed and isinstance(compacted_payload, dict):
                compacted_payload = {
                    **compacted_payload,
                    "suppressed_repeats": suppressed,
                }
        record = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "category": category,
            "message": message,
            "payload": compacted_payload,
        }
        serialized = self._serialize_record(record, payload)
        with self._write_lock:
            self._rotate_if_needed(target)
            with target.open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")
        return target

    @staticmethod
    def _dedupe_fingerprint(
        category: str,
        message: str,
        payload: Any,
    ) -> str:
        error_marker = ""
        if isinstance(payload, dict):
            error_marker = str(
                payload.get("error")
                or payload.get("error_type")
                or payload.get("message")
                or ""
            )
        return "\0".join((str(category), str(message), error_marker))

    def _serialize_record(self, record: dict[str, Any], original_payload: Any) -> str:
        serialized = json.dumps(record, ensure_ascii=False, default=str)
        serialized_size = len((serialized + "\n").encode("utf-8"))
        if serialized_size <= MAX_LOG_RECORD_BYTES:
            return serialized

        compacted_payload = record.get("payload")
        payload_summary: dict[str, Any] = {
            "truncated": True,
            "reason": "log record exceeded byte limit",
            "original_size_bytes": serialized_size,
            "payload_type": type(original_payload).__name__,
        }
        if isinstance(compacted_payload, dict):
            payload_summary["top_level_field_count"] = len(compacted_payload)
            payload_summary["top_level_keys"] = [
                self._compact_summary_key(key)
                for key in list(compacted_payload)[:MAX_PAYLOAD_ITEMS]
            ]
        elif isinstance(compacted_payload, list):
            payload_summary["item_count"] = len(compacted_payload)

        summary_record = {
            "time": record.get("time"),
            "category": self._compact_scalar(record.get("category")),
            "message": self._compact_scalar(record.get("message")),
            "payload": payload_summary,
        }
        summary_serialized = json.dumps(
            summary_record,
            ensure_ascii=False,
            default=str,
        )
        if len((summary_serialized + "\n").encode("utf-8")) <= MAX_LOG_RECORD_BYTES:
            return summary_serialized

        minimal_record = {
            "time": record.get("time"),
            "category": "core",
            "message": "oversized log record",
            "payload": {
                "truncated": True,
                "original_size_bytes": serialized_size,
            },
        }
        minimal_serialized = json.dumps(
            minimal_record,
            ensure_ascii=False,
            default=str,
        )
        if len((minimal_serialized + "\n").encode("utf-8")) <= MAX_LOG_RECORD_BYTES:
            return minimal_serialized
        return '{"truncated":true}'

    @staticmethod
    def _compact_summary_key(key: Any) -> str:
        key_text = str(key)
        if SENSITIVE_KEY_PATTERN.search(key_text):
            return "[REDACTED]"
        if len(key_text) > 128:
            return f"{key_text[:128]}... [truncated]"
        return key_text

    def _rotate_if_needed(self, target: Path) -> None:
        try:
            if not target.exists() or target.stat().st_size < MAX_LOG_FILE_BYTES:
                return

            oldest = target.with_name(f"{target.name}.{MAX_LOG_BACKUPS}")
            if oldest.exists():
                quarantine_path(
                    oldest,
                    self.logs_root / "recovery" / "log-retention",
                    operation="log-retention",
                    allowed_root=self.project_root,
                    original_path=oldest.relative_to(self.project_root).as_posix(),
                )

            for index in range(MAX_LOG_BACKUPS - 1, 0, -1):
                current = target.with_name(f"{target.name}.{index}")
                if current.exists():
                    current.replace(target.with_name(f"{target.name}.{index + 1}"))

            target.replace(target.with_name(f"{target.name}.1"))
        except (OSError, ValueError):
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
                key_text = str(key)
                compacted[key_text] = (
                    "[REDACTED]"
                    if SENSITIVE_KEY_PATTERN.search(key_text)
                    else self._compact_value(item, depth + 1)
                )
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
            text = CoreLogger._redact_environment_secrets(text)
            if len(text) > MAX_PAYLOAD_TEXT_CHARS:
                omitted = len(text) - MAX_PAYLOAD_TEXT_CHARS
                return f"{text[:MAX_PAYLOAD_TEXT_CHARS]}... [truncated {omitted} chars]"
            return text

        if isinstance(value, (int, float, bool)) or value is None:
            return value

        text = CoreLogger._redact_environment_secrets(str(value))
        if len(text) > MAX_PAYLOAD_TEXT_CHARS:
            omitted = len(text) - MAX_PAYLOAD_TEXT_CHARS
            return f"{text[:MAX_PAYLOAD_TEXT_CHARS]}... [truncated {omitted} chars]"
        return text

    @staticmethod
    def _redact_environment_secrets(text: str) -> str:
        redacted = text
        for key, value in os.environ.items():
            if (
                SENSITIVE_KEY_PATTERN.search(key)
                and isinstance(value, str)
                and len(value) >= 6
                and value in redacted
            ):
                redacted = redacted.replace(value, "[REDACTED]")
        return redacted

    # Logging-API compatible helpers for callers that expect stdlib logger methods.
    def debug(self, message: str, payload: Any | None = None) -> Path:
        return self.write("debug", message, payload)

    def info(self, message: str, payload: Any | None = None) -> Path:
        return self.write("info", message, payload)

    def warning(self, message: str, payload: Any | None = None) -> Path:
        return self.write("warning", message, payload)

    def error(self, message: str, payload: Any | None = None) -> Path:
        return self.write("error", message, payload)
