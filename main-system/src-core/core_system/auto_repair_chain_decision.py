"""Repair objective assigner — repair-chain-internal decision role.

This is NOT the governance-layer ``decision-sovereign`` (A152/A154).
It only assigns repair objectives based on health classification.
Does NOT execute repairs. Does NOT make permission decisions.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core_system.auto_repair_chain_types import (
    GovernanceAudit,
    HealthState,
    RepairObjective,
)


class RepairObjectiveAssigner:
    """Repair-chain-internal repair objective assigner.

    This is NOT the governance-layer ``decision-sovereign`` (A152/A154).
    It only assigns repair objectives based on health classification.
    Does NOT execute repairs. Does NOT make permission decisions.
    """

    def __init__(self, project_root: Path, audit: GovernanceAudit):
        self.project_root = project_root
        self.audit = audit

    def assign_repair_objective(
        self,
        health_classification: dict[str, Any],
        fault_code: str,
        root_cause_evidence: dict[str, Any],
    ) -> Optional[RepairObjective]:
        """Assign repair objective based on health classification.

        Returns None if no repair needed (healthy or containment only).
        """
        # Check if repair is warranted
        if health_classification["overall_state"] == HealthState.HEALTHY:
            return None

        # Build decision proof
        decision_proof = {
            "health_classification": health_classification,
            "fault_code": fault_code,
            "root_cause_evidence": root_cause_evidence,
            "decision_rule": "repair-responsibility-chain",
            "decided_by": "decision-sovereign",
            "decided_at": datetime.now(timezone.utc).isoformat(),
        }

        # Determine scope based on fault code and evidence
        scope = self._determine_scope(fault_code, root_cause_evidence)

        objective = RepairObjective(
            objective_id=f"obj_{uuid.uuid4().hex[:12]}",
            target_component=root_cause_evidence.get("component", "unknown"),
            fault_code=fault_code,
            root_cause_evidence=root_cause_evidence,
            decision_proof=decision_proof,
            scope=scope,
        )

        self.audit.record("repair_objective_assigned", {
            "objective_id": objective.objective_id,
            "fault_code": fault_code,
            "scope": scope,
            "decision_proof": decision_proof,
        })

        return objective

    def _determine_scope(self, fault_code: str, evidence: dict[str, Any]) -> dict[str, Any]:
        """Determine exact repair scope - no overreach."""
        scope = {
            "action": "patch",  # minimal by default
            "paths": [],
            "data_scopes": [],
            "max_files": 1,
            "max_lines_per_file": 50,
        }

        # Map fault codes to specific scopes
        if fault_code in ("MAIN_SYSTEM_SOURCE_SYNTAX_FAILED", "IndentationError", "TabError", "SyntaxError"):
            file_path = evidence.get("file", "")
            if file_path:
                scope["paths"] = [file_path]
                scope["action"] = "targeted_patch"
        elif fault_code in ("EXECUTABLE_MISSING", "PACKAGE_UNVERIFIED", "INCOMPATIBLE_TOOL_RUNTIME", "STALE_TOOL_PACKAGE", "SOURCE_RUNTIME_NOT_READY", "TOOL_RUNTIME_CRASH"):
            tool_id = evidence.get("tool_id", "")
            if tool_id:
                scope["action"] = "rebuild_artifact"
                scope["paths"] = [f"{tool_id}/"]
                scope["data_scopes"] = [f"{tool_id}/runtime/", f"{tool_id}/data/"]

        return scope


__all__ = ["RepairObjectiveAssigner"]
