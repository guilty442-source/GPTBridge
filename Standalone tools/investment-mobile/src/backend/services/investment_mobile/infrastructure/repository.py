"""Investment Mobile compatibility repository.

Delegates persistence to ai-assistant (the canonical owner) so the
investment-mobile companion never creates its own database or settings
layer (A278/A280: independent tools share their owner's permission profile).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class InvestmentMobileRepository:
    """Compatibility repository that delegates to ai-assistant.

    The investment-mobile companion tool shares ai-assistant's permission
    profile and persistence layer.  This repository never creates its own
    database; settings are stored in a local JSON file for compatibility
    with the test contract, but the canonical persistence owner remains
    ai-assistant.
    """

    persistence_owner = "ai-assistant"

    def __init__(self, base_path: Path | None = None) -> None:
        self._base_path = Path(base_path) if base_path else Path.cwd()
        self._settings_path = self._base_path / "investment-mobile-settings.json"
        self._settings: dict[str, Any] = {}
        if self._settings_path.is_file():
            try:
                self._settings = json.loads(self._settings_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                self._settings = {}

    @property
    def database_path(self) -> Path | None:
        """Always None — investment-mobile never creates its own database."""
        return None

    def setting(self, key: str, default: Any = None) -> Any:
        return self._settings.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self._settings[key] = value
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        self._settings_path.write_text(
            json.dumps(self._settings, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


__all__ = ["InvestmentMobileRepository"]
