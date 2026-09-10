from __future__ import annotations

import governance_rule.execution.git_tiers
from .audit_checks import audit_runtime_governance
from .audit_protected import REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES

governance_rule.execution.git_tiers.AUDIT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
governance_rule.execution.git_tiers.AUDIT_LEDGER_PATH.touch(exist_ok=True)


def main() -> int:
    errors = audit_runtime_governance()
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    print("[PASS] single-authority runtime governance")
    return 0


__all__ = ("audit_runtime_governance", "REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES", "main")