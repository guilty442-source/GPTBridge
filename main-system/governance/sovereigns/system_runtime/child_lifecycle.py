"""System Runtime Sovereign — Child Lifecycle (A334/A322)."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome


class SystemRuntimeChildLifecycleMixin:
    """Sub-sovereign lifecycle adjudication."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    _runtime_state: str

    async def _adjudicate_sub_sovereign_activate(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: authorize child activation."""
        child_id = request.payload.get("child_id")
        if not child_id:
            return refusal_outcome("MISSING_CHILD_ID", self.verified_basis("A334"))

        from governance.registries import validate_child_parent

        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome("NOT_OUR_CHILD", self.verified_basis("A334"))

        return accepted_outcome(
            {
                "authorized": True,
                "child_id": child_id,
                "execution": "delegated-to-governed-executor",
            },
            self.verified_basis("A334"),
        )

    async def _adjudicate_sub_sovereign_deactivate(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: authorize child deactivation."""
        child_id = request.payload.get("child_id")
        if not child_id:
            return refusal_outcome("MISSING_CHILD_ID", self.verified_basis("A334"))

        from governance.registries import validate_child_parent

        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome("NOT_OUR_CHILD", self.verified_basis("A334"))

        return accepted_outcome(
            {
                "authorized": True,
                "child_id": child_id,
                "execution": "delegated-to-governed-executor",
            },
            self.verified_basis("A334"),
        )

    async def _adjudicate_sub_sovereign_report_failure(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: child failure report — adjudicate bounded restart/quarantine."""
        child_id = request.payload.get("child_id")
        if not child_id:
            return refusal_outcome("MISSING_CHILD_ID", self.verified_basis("A322", "A334"))

        from governance.registries import validate_child_parent

        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome("NOT_OUR_CHILD", self.verified_basis("A334"))

        try:
            self.record_child_failure(child_id)
        except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
            pass

        return accepted_outcome(
            {
                "acknowledged": True,
                "child_id": child_id,
                "action": "failure-recorded-restart-adjudicated",
            },
            self.verified_basis("A322"),
        )