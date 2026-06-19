from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any


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
            "payload": payload or {},
        }

        file_name = "core.log"
        if category == "error":
            file_name = "error.log"
        elif category:
            file_name = f"{category}.log"

        target = self.logs_root / file_name
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        return target

    # Logging-API compatible helpers for callers that expect stdlib logger methods.
    def debug(self, message: str, payload: Any | None = None) -> Path:
        return self.write("debug", message, payload)

    def info(self, message: str, payload: Any | None = None) -> Path:
        return self.write("info", message, payload)

    def warning(self, message: str, payload: Any | None = None) -> Path:
        return self.write("warning", message, payload)

    def error(self, message: str, payload: Any | None = None) -> Path:
        return self.write("error", message, payload)
