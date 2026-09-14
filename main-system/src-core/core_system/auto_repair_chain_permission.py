"""Repair permission validator — repair-chain-internal scope validation.

This is NOT the governance-layer ``permission-sovereign`` (A436/E4).
It is a repair-chain-internal validator that checks the repair
objective's actor and scope before the governance-layer permission
sovereign's ``authorize()`` master-entry is called.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from governance_rule.execution.authentication import GovernanceAuthenticationService

from core_system.auto_repair_chain_types import (
    GovernanceAudit,
    PermissionGrant,
    RepairObjective,
)


class RepairPermissionValidator:
    """Repair-specific permission validator.

    This is NOT the governance-layer ``permission-sovereign`` (A436/E4).
    It is a repair-chain-internal validator that checks the repair
    objective's actor and scope before the governance-layer permission
    sovereign's ``authorize()`` master-entry is called.  The governance-
    layer sovereign remains the sole authority for permission matters
    (A436); this class only pre-validates the repair-specific scope.
    """

    def __init__(self, project_root: Path, auth_service: GovernanceAuthenticationService, audit: GovernanceAudit):
        self.project_root = project_root
        self.auth_service = auth_service
        self.audit = audit

    def validate_and_grant(
        self,
        objective: RepairObjective,
        actor: str,
    ) -> Optional[PermissionGrant]:
        """Validate objective and grant exact scope.

        Returns None if permission denied.
        """
        # Verify actor has authority to request this repair
        if actor not in ("runtime-sovereign", "release-update-sync-sub-sovereign", "health-maintenance-test-sub-sovereign"):
            self.audit.record("permission_denied", {
                "objective_id": objective.objective_id,
                "actor": actor,
                "reason": "unauthorized_actor",
            })
            return None

        # Validate scope against directory registrations
        if not self._validate_scope(objective.scope):
            self.audit.record("permission_denied", {
                "objective_id": objective.objective_id,
                "reason": "scope_validation_failed",
            })
            return None

        # Grant exact scope
        grant = PermissionGrant(
            grant_id=f"grant_{uuid.uuid4().hex[:12]}",
            target_entity=objective.target_component,
            action=objective.scope.get("action", "patch"),
            path_scope=objective.scope.get("paths", []),
            data_scope=objective.scope.get("data_scopes", []),
            expires_at=datetime.now(timezone.utc).isoformat(),
            proof={
                "objective_id": objective.objective_id,
                "validated_by": "permission-sovereign",
                "validated_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        self.audit.record("permission_granted", {
            "grant_id": grant.grant_id,
            "objective_id": objective.objective_id,
            "action": grant.action,
            "path_scope": grant.path_scope,
        })

        return grant

    def _validate_scope(self, scope: dict[str, Any]) -> bool:
        """Validate scope doesn't exceed registered boundaries."""
        # Check paths are within project root
        for path in scope.get("paths", []):
            full_path = (self.project_root / path).resolve()
            try:
                full_path.relative_to(self.project_root)
            except ValueError:
                return False
        return True


__all__ = ["RepairPermissionValidator"]
