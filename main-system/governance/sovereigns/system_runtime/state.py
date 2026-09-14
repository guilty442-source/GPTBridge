"""System Runtime Sovereign — State Persistence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class SystemRuntimeStateMixin:
    """State persistence for runtime-readiness.json."""

    _runtime_state: str

    def _load_state(self) -> dict[str, Any]:
        project_root = getattr(self.app, "project_root", None)
        if not project_root:
            return {}
        readiness_path = Path(project_root) / _READINESS_STATE_RELATIVE[0] / _READINESS_STATE_RELATIVE[1] / _READINESS_STATE_RELATIVE[2] / _READINESS_STATE_RELATIVE[3]
        try:
            payload = json.loads(readiness_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _save_state(self, payload: dict[str, Any]) -> None:
        project_root = getattr(self.app, "project_root", None)
        if not project_root:
            return
        readiness_path = Path(project_root) / _READINESS_STATE_RELATIVE[0] / _READINESS_STATE_RELATIVE[1] / _READINESS_STATE_RELATIVE[2] / _READINESS_STATE_RELATIVE[3]
        readiness_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = readiness_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, readiness_path)

    def _iso_now(self) -> str:
        return datetime.now(timezone.utc).isoformat()