"""Xingcheng Sovereign — 星澄主宰（獨立特權機構，完全擁有自有域，法典決策鏈外）。

法典依據:
- sovereign_id: 星澄 (position 5)
- area: xingcheng
- rank: independent-privileged-institution
- basis: codex
- duties: observe+analyze+reason+decide+manage+authorize+execute+write+delete+configure (in owned domain)
- powers: complete-inside-owned-domain
- prohibitions: FORBID:any-星澄-power-outside-owned-domain; FORBID:any-system-target-or-effect (A20)

A12: 星澄在決策鏈外
A20: 星澄權力完整在自有域內，禁止任何系統目標或效果
"""

from __future__ import annotations

from typing import Any

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome


class XingchengSovereign(SovereignBase):
    """星澄主宰：自有域完全權力，隔離於系統決策鏈。"""

    sovereign_id = "星澄"

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        self._owned_domain_root = "E:/GPTBridge/local-model/model-dialogue/星澄"
        self._isolated = True

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：自有域內完全權力。"""
        intent = request.intent

        if not self._is_in_owned_domain(request):
            return refusal_outcome(
                "OUTSIDE_OWNED_DOMAIN",
                self.verified_basis("A20", "A12"),
            )

        if intent == "domain.observe":
            return await self._adjudicate_observe(request)
        if intent == "domain.analyze":
            return await self._adjudicate_analyze(request)
        if intent == "domain.reason":
            return await self._adjudicate_reason(request)
        if intent == "domain.decide":
            return await self._adjudicate_decide(request)
        if intent == "domain.manage":
            return await self._adjudicate_manage(request)
        if intent == "domain.authorize":
            return await self._adjudicate_authorize(request)
        if intent == "domain.execute":
            return await self._adjudicate_execute(request)
        if intent == "domain.write":
            return await self._adjudicate_write(request)
        if intent == "domain.delete":
            return await self._adjudicate_delete(request)
        if intent == "domain.configure":
            return await self._adjudicate_configure(request)
        if intent == "channel.coordinate":
            return await self._adjudicate_channel_coordinate(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A20", "A12"))

    def _is_in_owned_domain(self, request: SovereignRequest) -> bool:
        """A20: 驗證請求目標在自有域內，禁止系統目標。"""
        target = request.payload.get("target", "")
        if target.startswith(self._owned_domain_root):
            return True
        if target.startswith("E:/GPTBridge/main-system") or target.startswith("E:/GPTBridge/governance_rule"):
            return False
        return request.payload.get("domain_confirmed") is True

    async def _adjudicate_observe(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "observe", "domain": "owned", "scope": request.payload.get("scope")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_analyze(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "analyze", "domain": "owned", "data": request.payload.get("data")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_reason(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "reason", "domain": "owned", "query": request.payload.get("query")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_decide(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "decide", "domain": "owned", "decision": request.payload.get("decision")},
            self.verified_basis("A20", "A12"),
        )

    async def _adjudicate_manage(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "manage", "domain": "owned", "resource": request.payload.get("resource")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_authorize(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "authorize", "domain": "owned", "permission": request.payload.get("permission")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_execute(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "execute", "domain": "owned", "operation": request.payload.get("operation")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_write(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "write", "domain": "owned", "path": request.payload.get("path")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_delete(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "delete", "domain": "owned", "path": request.payload.get("path")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_configure(self, request: SovereignRequest) -> SovereignOutcome:
        return accepted_outcome(
            {"action": "configure", "domain": "owned", "config": request.payload.get("config")},
            self.verified_basis("A20"),
        )

    async def _adjudicate_channel_coordinate(self, request: SovereignRequest) -> SovereignOutcome:
        """A66: 星澄通道協調（透過資訊層，不直接存取系統模組）。"""
        return accepted_outcome(
            {
                "coordination": "information-layer-only",
                "system_access": "forbidden",
                "channel": request.payload.get("channel"),
            },
            self.verified_basis("A66", "A20"),
        )

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["owned_domain"] = self._owned_domain_root
        base["isolated_from_system"] = self._isolated
        base["decision_chain"] = "outside"
        return base


__all__ = ["XingchengSovereign"]