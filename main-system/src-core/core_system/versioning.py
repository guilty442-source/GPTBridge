from __future__ import annotations

import functools
import json
import re
from pathlib import Path

APPLICATION_VERSION_PATTERN = re.compile(r"^\d+\.\d+$")

# Default project root: main-system/ (parent of src-core).
_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def application_version(project_root: Path | str | None = None) -> str:
    """Return the central GPTBridge product version from ``package.json``.

    This is the single dynamic source of truth for the product version.
    All component version constants should derive from this via
    :func:`component_version` rather than hardcoding their own strings.
    """

    root = Path(project_root) if project_root is not None else _DEFAULT_PROJECT_ROOT
    package_path = root / "package.json"
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
    """Return the version for a component, derived from the central version.

    All governed components share the product version from ``package.json``.
    ``component_id`` is accepted for API completeness and future per-component
    versioning, but currently returns the same central version for all.
    """

    root = Path(project_root) if project_root is not None else _DEFAULT_PROJECT_ROOT
    return _cached_version(str(root))


__all__ = [
    "APPLICATION_VERSION_PATTERN",
    "application_version",
    "component_version",
]
