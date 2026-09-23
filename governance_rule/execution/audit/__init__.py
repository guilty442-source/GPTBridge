"""Governance runtime audit — lazy entry point.

Importing this package (or any ``governance_rule.execution.audit.*``
submodule, e.g. ``native_audit_gate`` reached by the commit gate) must
not pull the check suite: the ~150-check Python oracle is loaded only
when ``main()`` runs or ``audit_runtime_governance`` is resolved.
"""
from __future__ import annotations

import os
from pathlib import Path


def _ensure_audit_ledger() -> None:
    """Create the git-tier audit ledger once an audit actually runs.

    ``git_tiers.audit_log`` also creates the file on first append, so
    nothing depends on it pre-existing; keeping the ensure here only
    preserves the historical "ledger exists after the audit ran"
    guarantee without paying an import-time filesystem touch (and
    without importing ``governance_rule.execution.git_tiers`` just to
    read one path constant).
    """
    ledger = Path(__file__).resolve().parent / "git_tier_audit.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.touch(exist_ok=True)


def _self_health_requested() -> bool:
    """Self-health (A57/E43) spawns subprocess test collection and is a
    release/startup gate concern.  The commit gate skips it by default
    (``GPTBRIDGE_AUDIT_SKIP_SELF_HEALTH=1``) to keep commits fast; the
    full barrier runs when the variable is unset or set to ``0``."""
    value = str(os.environ.get("GPTBRIDGE_AUDIT_SKIP_SELF_HEALTH", "0")).strip().casefold()
    return value not in {"1", "true", "yes", "on"}


def main() -> int:
    _ensure_audit_ledger()
    from .audit_checks import audit_runtime_governance

    errors = audit_runtime_governance(
        include_self_health=_self_health_requested(),
    )
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    print("[PASS] single-authority runtime governance")
    return 0


def __getattr__(name: str):
    """PEP 562 lazy exports — resolving an audit entry point is the
    moment the check suite is actually needed."""
    if name == "audit_runtime_governance":
        _ensure_audit_ledger()
        from .audit_checks import audit_runtime_governance

        return audit_runtime_governance
    if name == "REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES":
        from .audit_protected import REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES

        return REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ("audit_runtime_governance", "REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES", "main")
