from __future__ import annotations

from pathlib import Path


class InvestmentMobileRepository:
    """Non-persistent compatibility adapter required by the governed layer shape."""

    persistence_owner = "ai-assistant"
    database_path = None

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve()
        self._settings = {
            "enabled": "false",
            "allow_lan": "false",
            "port": "18765",
        }

    def setting(self, key: str, default: str) -> str:
        return self._settings.get(str(key), default)

    def set_setting(self, key: str, value: str) -> None:
        self._settings[str(key)] = str(value)


__all__ = ["InvestmentMobileRepository"]
