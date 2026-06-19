from __future__ import annotations

from typing import Any, Dict


class HighRiskModuleProtectionRule:
    """Protect critical runtime modules from destructive operations."""

    rule_id = "high_risk_module_protection"
    reason = "protected module cannot be deleted or moved by automation"

    PROTECTED_PREFIXES = (
        "src-core/providers",
        "src-core/orchestrator",
        "src-core/ipc",
        "src-core/governance",
        "src-core/settings",
    )

    def evaluate(self, operation: Dict[str, Any]) -> bool:
        action = str(operation.get("action", ""))
        target = str(operation.get("target", ""))
        actor = str(operation.get("actor", ""))

        if not any(target.replace('\\', '/').startswith(prefix) for prefix in self.PROTECTED_PREFIXES):
            return True

        if action in {"delete_file", "delete_folder", "move_file"}:
            self.reason = "destructive action is blocked on protected module"
            return False

        if actor in {"codex", "internal_ai_agent"} and action == "modify_file":
            # Allow manual workflows to proceed while still flagging risky writes.
            self.reason = "automated direct modification on protected module is blocked"
            return False

        return True
