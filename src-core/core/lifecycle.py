from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from core.paths import ensure_backup_layout, ensure_sandbox_layout


def initialize_core(project_root: str | Path | None = None) -> dict[str, Any]:
    """Prepare required GPTBridge runtime folders and return startup metadata."""

    root = Path(project_root or Path.cwd()).resolve()
    started_at = time.time()
    ensure_backup_layout(root)
    ensure_sandbox_layout(root)

    runtime_dirs = [
        root / "runtime",
        root / "runtime" / "logs",
        root / "runtime" / "state",
        root / "runtime" / "browser-profiles",
    ]
    for directory in runtime_dirs:
        directory.mkdir(parents=True, exist_ok=True)

    return {
        "ok": True,
        "project_root": str(root),
        "runtime_dirs": [str(path) for path in runtime_dirs],
        "initialized_at": started_at,
    }
