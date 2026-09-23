"""Governance root package — all exports resolve lazily (PEP 562).

Every submodule under ``governance_rule`` executes this file first, so
keeping it free of eager imports is what lets lightweight entry points
(the commit gate's native audit phase, single-submodule consumers) avoid
paying for the policy and permission-directory chains they never touch.
"""
from __future__ import annotations


def __getattr__(name: str):
    """Lazy compatibility exports (A279/A435).

    ``GOVERNANCE_CODEX`` is resolved on demand through the governed
    repository interface — importing ``governance_rule`` no longer
    performs an eager codex read.  Runtime viewers must enter through
    ``governance-codex://official`` (``codex_official``/``codex_session``).

    ``governance_policy_snapshot`` / ``directory_authority_snapshot``
    resolve on first use so importing any ``governance_rule.*`` submodule
    does not eagerly build the policy/permission-directory chains.
    """
    if name == "GOVERNANCE_CODEX":
        from governance_rule.execution.codex_repository import (
            load_governance_codex,
        )

        return load_governance_codex()
    if name == "governance_policy_snapshot":
        from .governance_policy import governance_policy_snapshot

        return governance_policy_snapshot
    if name == "directory_authority_snapshot":
        from .permission_directory import directory_authority_snapshot

        return directory_authority_snapshot
    if name in ("governance_policy", "permission_directory"):
        # Bare-submodule attribute access (``governance_rule.governance_policy``)
        # keeps working even though the eager binding is gone.
        import importlib

        return importlib.import_module(f".{name}", __name__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = (
    "GOVERNANCE_CODEX",
    "governance_policy_snapshot",
    "directory_authority_snapshot",
)
