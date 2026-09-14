"""Command Router — Governance Rules Handler."""

from __future__ import annotations

from typing import Any, Dict


class GovernanceRulesHandler:
    """Handle app:get-governance-rules command."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def handle(self, payload: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        rules = list(self.app.governance_rules)
        return "app:get-governance-rules_result", {
            "ok": True,
            "rules": rules,
            "available_rules": rules,
            "active_rules": rules,
            "authority": "governance-rule-only",
            "runtime_mutation": "prohibited",
            "command_strategy": {
                "model": "governance-authenticated-shared-layer-channel-only",
                "flow": [
                    "request",
                    "governance-authentication",
                    "shared-layer-queue",
                    "owner-tool-claim",
                    "owner-tool-execution",
                    "shared-layer-response",
                    "result",
                ],
                "global-cleaner": [
                    "global-garbage-cleanup",
                    "managed-backup-per-owner-retention",
                    "governed-backup-extraction",
                    "delete-excess-logs",
                    "read-only-system-health-check",
                ],
                "main-system-central-repair": [
                    "managed-storage-repair",
                    "write-repair-audit-and-log",
                ],
                "requester_write_authority": "none",
                "direct_main_to_tool_instruction": "PERMISSION_DENIED",
                "direct_tool_to_tool_instruction": "PERMISSION_DENIED",
                "automatic_repair": (
                    "governance-authorized-stability-only-with-optional-"
                    "global-cleaner-backup-extraction-via-shared-layer"
                ),
                "unauthorized_result": "PERMISSION_DENIED",
            },
        }