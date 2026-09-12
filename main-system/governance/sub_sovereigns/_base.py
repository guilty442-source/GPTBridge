"""Sub-Sovereign Base — 子主權底座（控制/調度在父權限下，無決策權、無執行權）。

法典依據:
- A64: SUB-SOVEREIGN: control/dispatch under parent authority; EXECUTION:governed-executor
- A284/A287: system-sub-sovereign: module-management-assignment-coordination-no-decision-no-execution
- A303/A304: startup-sub-sovereign: child-of-runtime-sovereign-no-decision-no-execution
- A308: language-review-sub-sovereign: child-of-permission-sovereign-no-decision-no-execution
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

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app)
        self._parent = parent
        self._managed_resources: dict[str, Any] = {}

    @property
    def parent(self) -> Any | None:
        return self._parent

    def set_parent(self, parent: Any) -> None:
        self._parent = parent

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """子主權裁決：僅協調/調度/管理，不決策、不執行。"""
        intent = request.intent

        if not self._verify_parent_authorization(request):
            return refusal_outcome("PARENT_AUTHORIZATION_REQUIRED", self.verified_basis("A64", "A284"))

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

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A64", "A284"))

    def _verify_parent_authorization(self, request: SovereignRequest) -> bool:
        return request.requester in (self.parent_sovereign_id, "decision-sovereign", "synchronization-sovereign", "permission-sovereign")

    async def _adjudicate_coordinate(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "coordinated": True,
                "scope": request.payload.get("scope"),
                "decision": "none",
                "execution": "none",
            },
            self.verified_basis("A64", "A284"),
        )

    async def _adjudicate_assign(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "assigned": request.payload.get("resource"),
                "to": request.payload.get("target"),
                "decision": "none",
                "execution": "none",
            },
            self.verified_basis("A64", "A284", "A287"),
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
            self.verified_basis("A64", "A284"),
        )

    async def _adjudicate_sync(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "synced": request.payload.get("target"),
                "decision": "none",
                "execution": "none",
            },
            self.verified_basis("A64", "A322"),
        )

    async def _adjudicate_status(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {
                "status": "active" if self.started else "stopped",
                "managed_resources": list(self._managed_resources.keys()),
                "parent": self.parent_sovereign_id,
            },
            self.verified_basis("A64"),
        )

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["parent"] = self.parent_sovereign_id
        base["managed_resources"] = list(self._managed_resources.keys())
        base["no_decision"] = True
        base["no_execution"] = True
        return base


__all__ = ["SubSovereignBase"]