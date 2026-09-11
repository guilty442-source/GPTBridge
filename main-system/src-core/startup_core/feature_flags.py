"""Feature flag system for parallel update safety.

Loads ``config/feature_flags.json`` and provides hot-reloadable flag
checks.  Flags can be toggled by editing the JSON file — the next
``is_enabled()`` call picks up the change without a process restart.

This enables:
  * Deploy new code without activating it (``enabled: false``).
  * Activate instantly by editing the config file (秒回滾 by reverting).
  * Gradual rollout via ``rollout_percentage``.

The loader is importable before ``_ensure_runtime_paths()`` because it
resolves the config file relative to its own ``__file__``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Final

_CONFIG_RELATIVE: Final[tuple[str, ...]] = (
    "..", "..", "config", "feature_flags.json",
)

_RELOAD_INTERVAL_SECONDS: Final[float] = 2.0


def _config_path() -> Path:
    return Path(__file__).resolve().parents[0].joinpath(*_CONFIG_RELATIVE)


class FeatureFlags:
    """Thread-safe, hot-reloadable feature flag registry."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: dict[str, Any] = {}
        self._last_load: float = 0.0
        self._last_mtime: float = 0.0
        self._load()

    def _load(self) -> None:
        path = _config_path()
        try:
            mtime = path.stat().st_mtime
            if mtime == self._last_mtime:
                return
            raw = json.loads(path.read_text(encoding="utf-8"))
            flags = raw.get("flags", {})
            with self._lock:
                self._cache = flags
                self._last_mtime = mtime
                self._last_load = time.monotonic()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            # Keep last known good cache; never crash on flag load failure.
            pass

    def _maybe_reload(self) -> None:
        if time.monotonic() - self._last_load < _RELOAD_INTERVAL_SECONDS:
            return
        self._load()

    def is_enabled(self, flag_name: str) -> bool:
        """Return True if the flag is enabled."""
        self._maybe_reload()
        with self._lock:
            entry = self._cache.get(flag_name, {})
            return bool(entry.get("enabled", False))

    def get(self, flag_name: str, key: str, default: Any = None) -> Any:
        """Return a specific field from a flag entry."""
        self._maybe_reload()
        with self._lock:
            entry = self._cache.get(flag_name, {})
            return entry.get(key, default)

    def get_entry(self, flag_name: str) -> dict[str, Any]:
        """Return the full flag entry."""
        self._maybe_reload()
        with self._lock:
            return dict(self._cache.get(flag_name, {}))

    def rollout_check(self, flag_name: str, bucket_key: str) -> bool:
        """Return True if this bucket should use the new path.

        Uses ``rollout_percentage`` (0-100) and a deterministic hash of
        ``bucket_key`` to decide.  When ``enabled`` is False, always
        returns False.
        """
        if not self.is_enabled(flag_name):
            return False
        percentage = self.get(flag_name, "rollout_percentage", 0)
        if percentage >= 100:
            return True
        if percentage <= 0:
            return False
        import hashlib
        h = int(hashlib.sha256(bucket_key.encode("utf-8")).hexdigest(), 16)
        return (h % 100) < int(percentage)

    def reload(self) -> None:
        """Force an immediate reload from disk."""
        self._last_mtime = 0.0
        self._load()


_flags: FeatureFlags | None = None
_flags_lock = threading.Lock()


def get_flags() -> FeatureFlags:
    """Return the singleton FeatureFlags instance."""
    global _flags
    if _flags is None:
        with _flags_lock:
            if _flags is None:
                _flags = FeatureFlags()
    return _flags


def is_enabled(flag_name: str) -> bool:
    """Module-level convenience: check if a flag is enabled."""
    return get_flags().is_enabled(flag_name)


__all__ = ["FeatureFlags", "get_flags", "is_enabled"]
