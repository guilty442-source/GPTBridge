from __future__ import annotations

import os

import governance_rule.execution.git_tiers
from .audit_checks import audit_runtime_governance
from .audit_protected import REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES

governance_rule.execution.git_tiers.AUDIT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
governance_rule.execution.git_tiers.AUDIT_LEDGER_PATH.touch(exist_ok=True)


def _self_health_requested() -> bool:
    """Self-health (A57/E43) spawns subprocess test collection and is a
    release/startup gate concern.  The commit gate skips it by default
    (``GPTBRIDGE_AUDIT_SKIP_SELF_HEALTH=1``) to keep commits fast; the
    full barrier runs when the variable is unset or set to ``0``."""
    value = str(os.environ.get("GPTBRIDGE_AUDIT_SKIP_SELF_HEALTH", "0")).strip().casefold()
    return value not in {"1", "true", "yes", "on"}


def main() -> int:
    errors = audit_runtime_governance(
        include_self_health=_self_health_requested(),
    )
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    print("[PASS] single-authority runtime governance")
    return 0


__all__ = ("audit_runtime_governance", "REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES", "main")