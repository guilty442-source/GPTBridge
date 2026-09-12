"""System Runtime Sovereign — 系統運行主宰（運行域決策，不執行）。

法典依據:
- sovereign_id: system-runtime-sovereign (position 9)
- area: system-runtime
- rank: runtime-domain-decision-only-no-execution
- basis: codex
- duties: process-survival|runtime-integrity|platform-serving
- powers: adjudicate-runtime-actions|coordinate-runtime-health
- prohibitions: FORBID:system-runtime-sub-sovereign-overstep-exec/codex (A28)
"""

from __future__ import annotations

from typing import Any

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome


class SystemRuntimeSovereign(SovereignBase):
    """系統運行主宰：進程存活、運行完整性、平台服務決策。"""

    sovereign_id = "system-runtime-sovereign"

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        self._runtime_state = "initializing"
        self._sub_sovereign: Any | None = None

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：運行動作、健康協調、子主宰管理。"""
        intent = request.intent

        if intent == "runtime.status":
            return await self._adjudicate_runtime_status(request)
        if intent == "runtime.action":
            return await self._adjudicate_runtime_action(request)
        if intent == "sub-sovereign.manage":
            return await self._adjudicate_sub_sovereign_manage(request)
        if intent == "health.coordinate":
            return await self._adjudicate_health_coordinate(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A28", "A12"))

    async def _adjudicate_runtime_status(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """運行狀態查詢。"""
        return accepted_outcome(
            {
                "runtime_state": self._runtime_state,
                "platform_serving": self._runtime_state == "serving",
                "process_survival": "monitored",
            },
            self.verified_basis("A28", "A65"),
        )

    async def _adjudicate_runtime_action(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A28: 運行動作裁決（不執行，委派執行器）。"""
        action = request.payload.get("action")
        if action not in {"start", "stop", "restart", "degraded", "recover"}:
            return refusal_outcome("INVALID_RUNTIME_ACTION", self.verified_basis("A28"))

        return accepted_outcome(
            {
                "authorized": True,
                "action": action,
                "execution": "delegated-to-runtime-sub-sovereign",
                "basis": "codex-delegation",
            },
            self.verified_basis("A28", "A63", "A64"),
        )

    async def _adjudicate_sub_sovereign_manage(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A284/A287: 系統模組管理/指派/協調（子主宰）。"""
        if self._sub_sovereign is None:
            return refusal_outcome("SUB_SOVEREIGN_NOT_REGISTERED", self.verified_basis("A284"))

        return accepted_outcome(
            {
                "delegated_to": "system-sub-sovereign",
                "action": request.payload.get("action", "coordinate"),
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A284", "A287", "A64"),
        )

    async def _adjudicate_health_coordinate(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """與維護主宰協調健康（A33/A65）。"""
        return accepted_outcome(
            {
                "coordinated_with": "maintenance-sovereign",
                "scope": "runtime-integrity",
                "information_layer": "official",
            },
            self.verified_basis("A28", "A33", "A65"),
        )

    def set_sub_sovereign(self, sovereign: Any) -> None:
        self._sub_sovereign = sovereign

    def set_runtime_state(self, state: str) -> None:
        self._runtime_state = state

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["runtime_state"] = self._runtime_state
        base["sub_sovereign"] = (
            self._sub_sovereign.live_status() if self._sub_sovereign else None
        )
        return base


__all__ = ["SystemRuntimeSovereign"]