"""Shared-layer version resolver — mechanical part of the version registry.

Reads the product version from ``main-system/package.json`` and derives
per-component versions.  This module is deliberately dependency-free:
it lives in the platform layer so ``shared_layer`` never imports
``core_system`` (P10 unit-boundary fix — previously
``channel_runtime.py`` imported ``core_system.versioning`` upward).

The governance wrapper (``CodeVersionPolicy`` authority basis and
``version_registry_status``) stays in ``core_system.versioning`` and
delegates here — mechanical down, governance up.
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path

APPLICATION_VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?$")

# Default project root: workspace root (parents: registry → shared_layer →
# src → shared-layer → workspace).  ``package.json`` lives under
# main-system/.
_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[4]


def application_version(project_root: Path | str | None = None) -> str:
    """Return the central GPTBridge product version from ``package.json``."""
    root = Path(project_root) if project_root is not None else _DEFAULT_PROJECT_ROOT
    package_path = root / "package.json"
    if not package_path.is_file():
        package_path = root / "main-system" / "package.json"
    try:
        version = str(
            json.loads(package_path.read_text(encoding="utf-8")).get("version") or ""
        ).strip()
    except OSError as error:
        raise RuntimeError("GPTBridge product version source is unavailable") from error
    except json.JSONDecodeError:
        version = ""
    if APPLICATION_VERSION_PATTERN.fullmatch(version) is None:
        raise RuntimeError(f"GPTBridge product version is invalid: {version or 'missing'}")
    return version


@functools.lru_cache(maxsize=64)
def _cached_version(project_root_str: str) -> str:
    return application_version(Path(project_root_str))


def component_version(component_id: str, project_root: Path | str | None = None) -> str:
    """Return the version for a component, derived from the central version."""
    root = Path(project_root) if project_root is not None else _DEFAULT_PROJECT_ROOT
    return _cached_version(str(root))


def refresh_version_cache() -> None:
    """Clear the version cache so the next read re-resolves from source."""
    _cached_version.cache_clear()


__all__ = [
    "APPLICATION_VERSION_PATTERN",
    "application_version",
    "component_version",
    "refresh_version_cache",
]
