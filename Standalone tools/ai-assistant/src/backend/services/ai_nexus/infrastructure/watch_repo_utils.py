from __future__ import annotations

from typing import Any


class WatchRepoUtilsMixin:
    """Shared static utility methods used across watch repository mixins."""

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + "\n...truncated..."

    @staticmethod
    def _int_value(value: Any, default: int = 0) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _compact_string_list(value: Any, limit: int) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if str(item or "").strip()][:limit]
