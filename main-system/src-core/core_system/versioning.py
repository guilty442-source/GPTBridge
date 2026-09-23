"""Version registry — central version authority for GPTBridge components.

The version source is governed by the Permission Sovereign via the
Directory Authority's ``CodeVersionPolicy``
(``version_source="codex-or-tool-manifest"``).  The mechanical resolver
(reading ``main-system/package.json``) lives in
``shared_layer.versioning`` — the platform layer — so lower layers never
import upward.  This module keeps the governance wrapper: the policy
basis and ``version_registry_status``.

All component version constants should derive from this via
:func:`component_version` rather than hardcoding their own strings.

Authority basis: ``CODE_VERSION_POLICY`` from the permission directory.
Physical source: ``main-system/package.json`` (the main-system manifest).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from shared_layer.registry.versioning import (
    APPLICATION_VERSION_PATTERN,
    application_version,
    component_version,
    refresh_version_cache,
)


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
