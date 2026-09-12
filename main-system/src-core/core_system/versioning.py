"""Version registry — central version authority for GPTBridge components.

The version source is governed by the Permission Sovereign via the
Directory Authority's ``CodeVersionPolicy``
(``version_source="codex-or-tool-manifest"``).  This module
is the single dynamic resolver that reads the physical version source
(``main-system/package.json``) under that policy.

All component version constants should derive from this via
:func:`component_version` rather than hardcoding their own strings.

Authority basis: ``CODE_VERSION_POLICY`` from the permission directory.
Physical source: ``main-system/package.json`` (the main-system manifest).
"""

from __future__ import annotations

import functools
import json
import re
from pathlib import Path
from typing import Any

APPLICATION_VERSION_PATTERN = re.compile(r"^\d+\.\d+(?:\.\d+)?$")

# Default project root: main-system/ (parent of src-core).
_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _code_version_policy() -> Any:
    """Return the CodeVersionPolicy from the permission directory.

    Lazy import avoids circular imports at module load time.  The policy
    is the authority basis for the version registry — it declares the
    version source, scope, and update rules.
    """

    from governance_rule.permission_directory.directory_authority import (
        CODE_VERSION_POLICY,
    )
    return CODE_VERSION_POLICY


def application_version(project_root: Path | str | None = None) -> str:
    """Return the central GPTBridge product version from ``package.json``.

    This is the single dynamic source of truth for the product version,
    resolved under the Permission Sovereign's ``CodeVersionPolicy``.
    """

    root = Path(project_root) if project_root is not None else _DEFAULT_PROJECT_ROOT
    package_path = root / "package.json"
    if not package_path.is_file():
        # ``package.json`` moved from the workspace root to ``main-system/``
        # (commit 559c0a4).  When ``project_root`` is the workspace root
        # (e.g. via ``GPTBRIDGE_PROJECT_ROOT``), fall back to the
        # ``main-system/`` subdirectory where the manifest now lives.
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
    """Return the version for a component, derived from the central version.

    All governed components share the product version from ``package.json``
    under the Permission Sovereign's ``CodeVersionPolicy``.  ``component_id``
    is accepted for registry bookkeeping and future per-component versioning.
    """

    root = Path(project_root) if project_root is not None else _DEFAULT_PROJECT_ROOT
    return _cached_version(str(root))


def refresh_version_cache() -> None:
    """Clear the version cache so the next read re-resolves from source.

    Called by the Permission Sovereign's ``re_certify`` flow after a codex
    amendment or directory re-seal, ensuring the version registry reflects
    the current authority state.
    """

    _cached_version.cache_clear()


def version_registry_status() -> dict[str, Any]:
    """Return the version registry snapshot governed by the Permission Sovereign.

    This exposes the ``CodeVersionPolicy`` authority basis alongside the
    current resolved version, so the Permission Sovereign can surface the
    version directory as part of its coordination status.
    """

    policy = _code_version_policy()
    return {
        "authority": "permission-sovereign",
        "authority_basis": "code-version-policy",
        "version_source": policy.version_source,
        "scope": policy.scope,
        "initial_version": policy.initial_version,
        "explicit_target_version_required": policy.explicit_target_version_required,
        "unversioned_update": policy.unversioned_update,
        "same_version_update": policy.same_version_update,
        "hot_update_requires_version_change": policy.hot_update_requires_version_change,
        "failure_code": policy.failure_code,
        "current_version": application_version(),
    }


__all__ = [
    "APPLICATION_VERSION_PATTERN",
    "application_version",
    "component_version",
    "refresh_version_cache",
    "version_registry_status",
]
