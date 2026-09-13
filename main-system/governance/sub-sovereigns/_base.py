"""Sub-Sovereign Base — 子主權底座（控制/調度在父權限下，無決策權、無執行權）。

法典依據:
- A64: SUB-SOVEREIGN: control/dispatch under parent authority; EXECUTION:governed-executor
- A284/A287: system-sub-sovereign: module-management-assignment-coordination-no-decision-no-execution
- A303/A304: startup-sub-sovereign: child-of-runtime-sovereign-no-decision-no-execution
- A308: language-review-sub-sovereign: ABOLISHED per A334; capability transferred to 星澄
- A316: directory-sub-sovereign: child-of-permission-sovereign-no-decision-no-review-no-execution
- A317: identity-group-sub-sovereign: child-of-permission-sovereign-no-decision-no-review-no-execution
- A322: all sync sub-sovereigns: child-of-synchronization-sovereign-no-decision-no-execution
- A323: policy-architecture/health-maintenance-test/data-governance/priority-capability/change-acceptance sub-sovereigns: child-of-decision-sovereign-no-decision-no-execution
- A327: dependency-sync-sub-sovereign: child-of-synchronization-sovereign-single-duty-no-decision-no-execution
"""

from __future__ import annotations

from abc import ABC
from typing import Any

from ..sovereigns._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome


class SubSovereignBase(SovereignBase, ABC):
    """子主權底座：無決策權、無執行權、僅控制/調度。"""

    parent_sovereign_id: str = ""

    # A10/A11 explicit intent allowlist — a sub-sovereign only accepts the
    # coordination intents declared by its adjudication surface; the base
    # edict-ID check would reject all of them (fail-closed).
    _INTENT_ALLOWLIST: frozenset[str] = frozenset(
        {"coordinate", "assign", "manage", "sync", "status"}
    )

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app)
        self._parent = parent
        self._managed_resources: dict[str, Any] = {}
        self._assignments: dict[str, dict[str, Any]] = {}
        # Per-target sync bookkeeping — named ``_sync_targets`` because
        # several domain subclasses already use ``_sync_state`` for their
        # own domain sync payload.
        self._sync_targets: dict[str, dict[str, Any]] = {}
        self._coordinations: list[dict[str, Any]] = []

    @property
    def parent(self) -> Any | None:
        return self._parent

    def set_parent(self, parent: Any) -> None:
        self._parent = parent

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """子主權裁決：僅協調/調度/管理，不決策、不執行。"""
        intent = request.intent

        if not self._verify_parent_authorization(request):
            return refusal_outcome("PARENT_AUTHORIZATION_REQUIRED", self.verified_basis("A130", "A284"))

        if intent == "coordinate":
            return await self._adjudicate_coordinate(request)
        if intent == "assign":
            return await self._adjudicate_assign(request)
        if intent == "manage":
            return await self._adjudicate_manage(request)
        if intent == "sync":
            return await self._adjudicate_sync(request)
        if intent == "status":
            return await self._adjudicate_status(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A130", "A284"))

    def _verify_intent(self, intent: str) -> bool:
        """A10/A11 fail-closed: only declared coordination intents pass."""
        return intent in self._INTENT_ALLOWLIST

    def _verify_parent_authorization(self, request: SovereignRequest) -> bool:
        """A334: each sub-sovereign has exactly one codex-registered parent.

        The request must arrive *through* that parent: ``requester`` must
        equal the codex parent AND the payload must carry the
        ``_delegated_by`` stamp that ``SovereignBase.delegate_to`` adds.
        A bare ``requester=<parent>`` without the stamp is refused —
        in-process callers may not impersonate the parent by string
        alone.
        """
        from ..registries import parent_of

        parent = parent_of(self.sovereign_id)
        if request.requester != parent:
            return False
        return request.payload.get("_delegated_by") == parent

    def report_to_parent(self, kind: str) -> bool:
        """Report a lifecycle outcome to the codex-registered parent.

        A334 fail-closed: the report only reaches the single codex parent —
        ``parent_sovereign_id`` must match ``sovereign_hierarchy_registry``
        and the parent must be materialized.  ``success``/``recovered``/
        ``converged`` clear the parent's consecutive-failure counter; any
        other kind records a failure.  Returns False when delivery failed
        so the governed executor can treat it explicitly.
        """
        from ..registries import parent_of, resolve_sovereign

        if parent_of(self.sovereign_id) != self.parent_sovereign_id:
            return False
        parent = resolve_sovereign(self.app, self.parent_sovereign_id)
        if parent is None:
            return False
        if str(kind).casefold() in ("success", "recovered", "converged"):
            parent.record_child_success(self.sovereign_id)
        else:
            parent.record_child_failure(self.sovereign_id)
        return True

    async def _adjudicate_coordinate(self, request: SovereignRequest) -> SovereignOutcome:
        event = {
            "scope": request.payload.get("scope"),
            "detail": request.payload.get("detail"),
            "coordinated_at": self._iso_now(),
            "event_id": f"coord-{len(self._coordinations) + 1}",
        }
        self._coordinations.append(event)
        del self._coordinations[:-200]
        return accepted_outcome(
            {
                "coordinated": True,
                "event_id": event["event_id"],
                "scope": event["scope"],
                "decision": "none",
                "execution": "none",
            },
            self.verified_basis("A130", "A284"),
        )

    async def _adjudicate_assign(self, request: SovereignRequest) -> SovereignOutcome:
        """A334: module assignments are validated against the codex
        module_assignment_registry — a sub-sovereign may only coordinate
        modules whose ``managing_sub_sovereign`` is itself."""
        module_code = request.payload.get("module") or request.payload.get("resource")
        if module_code:
            from ..registries import module_assignment

            row = module_assignment(str(module_code))
            if row is None:
                return refusal_outcome(
                    "MODULE_NOT_IN_REGISTRY", self.verified_basis("A334")
                )
            if row.get("managing_sub_sovereign") != self.sovereign_id:
                return refusal_outcome(
                    "MODULE_ASSIGNED_ELSEWHERE",
                    self.verified_basis("A334"),
                )
            self._assignments[str(module_code)] = {
                "to": request.payload.get("target"),
                "primary_domain": row.get("primary_domain"),
                "assigned_at": self._iso_now(),
            }
        return accepted_outcome(
            {
                "assigned": module_code,
                "to": request.payload.get("target"),
                "recorded": module_code in self._assignments,
                "decision": "none",
                "execution": "none",
            },
            self.verified_basis("A130", "A284", "A287", "A334"),
        )

    async def _adjudicate_manage(self, request: SovereignRequest) -> SovereignOutcome:
        resource = request.payload.get("resource")
        action = request.payload.get("action", "monitor")

        if resource:
            self._managed_resources[resource] = {
                "action": action,
                "managed_at": self._iso_now(),
            }

        return accepted_outcome(
            {
                "managed": resource,
                "action": action,
                "decision": "none",
                "execution": "delegated-to-governed-executor",
            },
            self.verified_basis("A130", "A284"),
        )

    async def _adjudicate_sync(self, request: SovereignRequest) -> SovereignOutcome:
        target = request.payload.get("target")
        if target:
            self._sync_targets[str(target)] = {
                "status": request.payload.get("sync_status") or "synced",
                "synced_at": self._iso_now(),
            }
        return accepted_outcome(
            {
                "synced": target,
                "sync_state": self._sync_targets.get(str(target)),
                "decision": "none",
                "execution": "none",
            },
            self.verified_basis("A130", "A322"),
        )

    async def _adjudicate_status(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "status": "active" if self.started else "stopped",
                "managed_resources": list(self._managed_resources.keys()),
                "assignments": list(self._assignments.keys()),
                "sync_state": dict(self._sync_targets),
                "parent": self.parent_sovereign_id,
            },
            self.verified_basis("A130"),
        )

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["parent"] = self.parent_sovereign_id
        base["managed_resources"] = list(self._managed_resources.keys())
        base["assignments"] = list(self._assignments.keys())
        base["pending_sync"] = [
            target
            for target, state in self._sync_targets.items()
            if state.get("status") != "synced"
        ]
        base["no_decision"] = True
        base["no_execution"] = True
        return base


__all__ = ["SubSovereignBase"]