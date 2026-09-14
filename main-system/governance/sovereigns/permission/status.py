"""Permission Sovereign — Status Surfaces."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase


class PermissionStatusMixin:
    """Status reporting surfaces."""

    _issued_grants: dict[str, dict[str, Any]]
    _compliance_violations: list[dict[str, Any]]
    _automation: Any
    _governance_ref: Any
    _started: bool

    def status(self) -> dict[str, Any]:
        return self._with_status_schema({
            "issued_grants": len(self._issued_grants),
            "compliance_violations": len(self._compliance_violations),
            "automation": self._get_automation_status(),
        })

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["grants"] = dict(self._issued_grants)
        base["violations"] = list(self._compliance_violations)
        return base

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "state": "decision-only",
            "owner": self.sovereign_id,
            "issued_grants": len(self._issued_grants),
            "compliance_violations": len(self._compliance_violations),
        }

    def _get_automation_status(self) -> dict[str, Any]:
        if self._automation is None:
            return {"enabled": False}
        return self._automation.status()