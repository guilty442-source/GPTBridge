"""Synchronization Sovereign — A322 Sync Decision Adjudication.

Sole decision authority over sync target, dependency order, atomic boundary,
conflict isolation, retry/cancel, and convergence acceptance.
"""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome, verified_basis


class SyncA322DecisionMixin:
    """A322 sync decision adjudication (no direct execution)."""

    _sub_sovereigns: dict[str, Any]
    app: Any

    async def _adjudicate_sync_decision(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: adjudicate sync decision — SOLE decision authority."""
        decision_type = request.payload.get("decision_type")
        if not decision_type:
            return refusal_outcome("MISSING_DECISION_TYPE", verified_basis(("A322",)))

        from governance.registries import children_of

        if decision_type == "sync-target":
            # Adjudicate which child handles the sync target
            target = request.payload.get("target")
            if not target:
                return refusal_outcome("MISSING_TARGET", verified_basis(("A322",)))

            for child_id in children_of("synchronization-sovereign"):
                domain = primary_domain_of(child_id) if 'primary_domain_of' in dir() else None
                if domain and target in str(domain):
                    return accepted_outcome(
                        {
                            "decision": "sync-target-assigned",
                            "child_id": child_id,
                            "target": target,
                        },
                        verified_basis(("A322",)),
                    )

        elif decision_type == "dependency-order":
            # Adjudicate dependency order for multi-child sync
            targets = request.payload.get("targets", [])
            if not isinstance(targets, list):
                return refusal_outcome("INVALID_TARGETS", verified_basis(("A322",)))

            order = []
            for target in targets:
                for child_id in children_of("synchronization-sovereign"):
                    domain = primary_domain_of(child_id) if 'primary_domain_of' in dir() else None
                    if domain and target in str(domain):
                        order.append(child_id)
                        break

            return accepted_outcome(
                {
                    "decision": "dependency-order-resolved",
                    "order": order,
                },
                verified_basis(("A322",)),
            )

        elif decision_type == "conflict-disposition":
            # Adjudicate conflict between sync operations
            conflict = request.payload.get("conflict")
            if not conflict:
                return refusal_outcome("MISSING_CONFLICT", verified_basis(("A322",)))

            return accepted_outcome(
                {
                    "decision": "conflict-disposition",
                    "disposition": "sequential-serialized",
                    "rationale": "A322 atomic boundary + conflict isolation",
                },
                verified_basis(("A322",)),
            )

        elif decision_type == "retry-cancel":
            # Adjudicate retry or cancel for failed sync
            child_id = request.payload.get("child_id")
            if not child_id:
                return refusal_outcome("MISSING_CHILD_ID", verified_basis(("A322",)))

            attempts = int(request.payload.get("attempts", 0))
            max_attempts = int(request.payload.get("max_attempts", 3))

            if attempts >= max_attempts:
                return accepted_outcome(
                    {
                        "decision": "cancel",
                        "child_id": child_id,
                        "reason": "max-attempts-exceeded",
                    },
                    verified_basis(("A322",)),
                )
            else:
                return accepted_outcome(
                    {
                        "decision": "retry",
                        "child_id": child_id,
                        "attempt": attempts + 1,
                    },
                    verified_basis(("A322",)),
                )

        elif decision_type == "convergence-acceptance":
            # Adjudicate convergence acceptance
            results = request.payload.get("results", [])
            if not isinstance(results, list):
                return refusal_outcome("INVALID_RESULTS", verified_basis(("A322",)))

            all_converged = all(
                isinstance(r, dict) and r.get("converged", False)
                for r in results
            )

            return accepted_outcome(
                {
                    "decision": "convergence-acceptance",
                    "accepted": all_converged,
                    "results": results,
                },
                verified_basis(("A322",)),
            )

        return refusal_outcome("UNKNOWN_DECISION_TYPE", verified_basis(("A322",)))