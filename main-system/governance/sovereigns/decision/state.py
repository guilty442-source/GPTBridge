"""Decision Sovereign — State Persistence.

Load/save decision-sovereign.json state file.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class DecisionStateMixin:
    """State persistence for decision-sovereign.json."""

    runtime_state_path: Path
    workspace_root: Path

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.runtime_state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}