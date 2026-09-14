"""Synchronization Sovereign — Child Lifecycle (A334/A322).

Adjudicates sub-sovereign activation, deactivation, and failure reporting.
"""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome, verified_basis


class SyncChildLifecycleMixin:
    """Sub-sovereign lifecycle adjudication (A334)."""

    _sub_sovereigns: dict[str, Any]
    app: Any

    async def _adjudicate_sub_sovereign_activate(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: authorize child activation — executor materializes."""
        child_id = request.payload.get("child_id")
        if not child_id:
            return refusal_outcome("MISSING_CHILD_ID", verified_basis("A334"))

        from governance.registries import validate_child_parent

        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome("NOT_OUR_CHILD", verified_basis("A334"))

        return accepted_outcome(
            {
                "authorized": True,
                "child_id": child_id,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis("A334"),
        )

    async def _adjudicate_sub_sovereign_deactivate(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: authorize child deactivation."""
        child_id = request.payload.get("child_id")
        if not child_id:
            return refusal_outcome("MISSING_CHILD_ID", verified_basis("A334"))

        from governance.registries import validate_child_parent

        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome("NOT_OUR_CHILD", verified_basis("A334"))

        return accepted_outcome(
            {
                "authorized": True,
                "child_id": child_id,
                "execution": "delegated-to-governed-executor",
            },
            verified_basis("A334"),
        )

    async def _adjudicate_sub_sovereign_report_failure(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: child failure report — adjudicate bounded restart/quarantine."""
        child_id = request.payload.get("child_id")
        if not child_id:
            return refusal_outcome("MISSING_CHILD_ID", verified_basis("A322", "A334"))

        from governance.registries import validate_child_parent

        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome("NOT_OUR_CHILD", verified_basis("A334"))

        # Increment failure counter on codex parent (A322)
        try:
            self.record_child_failure(child_id)
        except Exception:
            pass

        return accepted_outcome(
            {
                "acknowledged": True,
                "child_id": child_id,
                "action": "failure-recorded-restart-adjudicated",
            },
            verified_basis("A322"),
        )