"""§10.27 / §10.63 R5 — resident-core manifest loader.

Reads ``main-system/config/resident-core.json`` and exposes the
residency mode of each boot-time component:

- ``resident-core`` — starts at boot (IPC, governance gates, runtime
  state, request registry, bounded health monitoring, the shared
  periodic scheduler).
- ``on-demand`` — capability stays available but the component is not
  resident; it is activated by its manifest-declared trigger.
- ``scheduled`` — runs via the shared PeriodicScheduler, not a
  dedicated loop.

The loader resolves the config relative to its own ``__file__`` so it is
importable before ``_ensure_runtime_paths()``.  A missing or corrupt
manifest fails safe to ``resident-core`` — the status-quo behaviour —
so a broken config can never silently disable a governed component.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

_CONFIG_RELATIVE: Final[tuple[str, ...]] = (
    "..", "..", "config", "resident-core.json",
)

RESIDENT_CORE: Final = "resident-core"
ON_DEMAND: Final = "on-demand"
SCHEDULED: Final = "scheduled"


def _config_path() -> Path:
    return Path(__file__).resolve().parents[0].joinpath(*_CONFIG_RELATIVE)


@lru_cache(maxsize=1)
def _manifest() -> dict[str, Any]:
    try:
        raw = json.loads(_config_path().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    components = raw.get("components")
    return components if isinstance(components, dict) else {}


def resident_mode(component: str) -> str:
    """Return the residency mode for ``component`` (fail-safe: resident)."""
    entry = _manifest().get(component)
    if not isinstance(entry, dict):
        return RESIDENT_CORE
    mode = entry.get("mode")
    return mode if mode in (RESIDENT_CORE, ON_DEMAND, SCHEDULED) else RESIDENT_CORE


def manifest_components() -> dict[str, Any]:
    """Return the raw component classification map (observability)."""
    return dict(_manifest())


__all__ = [
    "ON_DEMAND",
    "RESIDENT_CORE",
    "SCHEDULED",
    "manifest_components",
    "resident_mode",
]
