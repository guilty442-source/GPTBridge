"""System Runtime Sovereign — 系統運行主宰（運行域決策，不執行）。

法典依據:
- sovereign_id: system-runtime-sovereign (position 9)
- area: system-runtime
- rank: runtime-domain-decision-only-no-execution
- basis: codex
- duties: process-survival|runtime-integrity|platform-serving
- powers: adjudicate-runtime-actions|coordinate-runtime-health
- prohibitions: FORBID:runtime-state-sync-sub-sovereign-overstep-exec/codex (A28)

Full-automation upgrade (A28/A33/A65):
- Background auto-loop monitors coverage, runtime readiness, child
  health, process survival, and convergence.
- Coverage gaps and runtime degradation are auto-routed to the
  decision-sovereign's repair-decision chain (A152/A154).
- The sovereign remains decision-only: it adjudicates and routes; it
  never executes.  All adjudications go through ``handle()`` (A10/A11
  fail-closed).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from ._delegation import record_delegation_outcome
from core_system.codex_decision import accepted_outcome, refusal_outcome

from ..registries import (
    children_of,
    module_assignment,
    parent_of,
    primary_domain_of,
    validate_child_parent,
)

from .system_runtime.child_access import SystemRuntimeChildAccessMixin
from .system_runtime.intent_adjudication import SystemRuntimeIntentMixin
from .system_runtime.child_lifecycle import SystemRuntimeChildLifecycleMixin
from .system_runtime.module_routing import SystemRuntimeModuleRoutingMixin
from .system_runtime.a322_decisions import SystemRuntimeA322DecisionMixin
from .system_runtime.autonomy import SystemRuntimeAutonomyMixin
from .system_runtime.status import SystemRuntimeStatusMixin
from .system_runtime.state import SystemRuntimeStateMixin

_logger = logging.getLogger("gptbridge.sovereign.system_runtime")

# Bounded restart budget for child failure adjudication.
_MAX_CHILD_RESTARTS = 3

# Runtime readiness state file (information-layer, A67).
_READINESS_STATE_RELATIVE = (
    "main-system",
    "runtime",
    "state",
    "runtime-readiness.json",
)

# Terminal runtime states that trigger repair routing.
_DEGRADED_STATES = frozenset({"degraded", "failed", "dead", "unknown"})
_SERVING_STATES = frozenset({"serving", "ready", "active", "converged"})


class SystemRuntimeSovereign(
    SystemRuntimeChildAccessMixin,
    SystemRuntimeIntentMixin,
    SystemRuntimeChildLifecycleMixin,
    SystemRuntimeModuleRoutingMixin,
    SystemRuntimeA322DecisionMixin,
    SystemRuntimeAutonomyMixin,
    SystemRuntimeStatusMixin,
    SystemRuntimeStateMixin,
    SovereignBase,
):
    """系統運行主宰：進程存活、運行完整性、平台服務決策。"""

    sovereign_id = "system-runtime-sovereign"

    # A10/A11 explicit intent allowlist
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        "runtime.status",
        "runtime.action",
        "sub-sovereign.manage",
        "health.coordinate",
        # Child lifecycle (A334)
        "sub-sovereign.activate",
        "sub-sovereign.deactivate",
        "sub-sovereign.report-failure",
        # Module routing (A334)
        "module.route",
        # A322-style retry/cancel and convergence
        "runtime.retry-cancel",
        "runtime.convergence-acceptance",
        # Conflict isolation
        "runtime.conflict-isolation",
    })

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        self._runtime_state = "initializing"
        # Auto-automation state (A28/A33/A65 full-automation upgrade).
        self._auto_loop_task: asyncio.Task[Any] | None = None
        self._auto_loop_interval: float = 5.0  # seconds
        self._auto_enabled: bool = True
        # Automation metrics for status surfaces.
        self._auto_metrics: dict[str, Any] = {
            "coverage_checks": 0,
            "gap_repairs_routed": 0,
            "child_retries_triggered": 0,
            "child_quarantines": 0,
            "runtime_state_polls": 0,
            "degradation_detected": 0,
            "health_coordinations": 0,
        }
        # Child supervision watch: started/stopped transitions, restart
        # attempts and quarantine markers decided by this sovereign.
        self._child_supervision: dict[str, dict[str, Any]] = {}
        # Sub-sovereign registry — populated by the governed executor at activation.
        self._sub_sovereigns: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Intent gate (A10/A11 explicit allowlist)
    # ------------------------------------------------------------------

    def _verify_intent(self, intent: str) -> bool:
        """Override base-class edict-ID check with explicit intent allowlist (A10/A11 fail-closed)."""
        return intent in self._INTENT_ALLOWLIST

    # ------------------------------------------------------------------
    # Single-gate adjudication (A10/A11)
    # ------------------------------------------------------------------

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：運行狀態、動作、子主宰管理、健康協調、模組路由、A322決策。"""
        intent = request.intent

        if intent == "runtime.status":
            return await self._adjudicate_runtime_status(request)
        if intent == "runtime.action":
            return await self._adjudicate_runtime_action(request)
        if intent == "sub-sovereign.manage":
            return await self._adjudicate_sub_sovereign_manage(request)
        if intent == "health.coordinate":
            return await self._adjudicate_health_coordinate(request)
        if intent == "sub-sovereign.activate":
            return await self._adjudicate_sub_sovereign_activate(request)
        if intent == "sub-sovereign.deactivate":
            return await self._adjudicate_sub_sovereign_deactivate(request)
        if intent == "sub-sovereign.report-failure":
            return await self._adjudicate_sub_sovereign_report_failure(request)
        if intent == "module.route":
            return await self._adjudicate_module_route(request)
        if intent == "runtime.retry-cancel":
            return await self._adjudicate_retry_cancel(request)
        if intent == "runtime.convergence-acceptance":
            return await self._adjudicate_convergence_acceptance(request)
        if intent == "runtime.conflict-isolation":
            return await self._adjudicate_conflict_isolation(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A10", "A12"))

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """系統運行主宰委派執行（A69/A121）。

        This sovereign is decision-only (A28).  Execution is delegated
        to governed executor / sub-sovereigns via delegate_to.  This hook
        attests that and records the delegation outcome in the audit ledger.
        """
        record_delegation_outcome(
            sovereign_id=self.sovereign_id,
            intent=request.intent,
            requester=request.requester,
            accepted=decision.accepted,
            reason_code=decision.refusal.reason_code if decision.refusal else "",
            execution_mode="decision-only",
            basis=decision.basis,
        )
        return decision

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """啟動系統運行主宰（決策層初始化，A297）。

        Decision-layer initialization only: the supervision loop is
        started separately by ``start_supervision()``.
        """
        state = await super().start()
        app_registry = getattr(self.app, "_sub_sovereigns", None)
        if isinstance(app_registry, dict):
            app_registry.update(self._sub_sovereigns)
        state["sub_sovereigns"] = list(self._sub_sovereigns.keys())
        return state

    async def start_supervision(self) -> None:
        """Start the autonomous supervision loop (A297 separation)."""
        self._start_autonomy_loop()

    async def stop_supervision(self) -> None:
        """Stop the autonomous supervision loop (A297 separation)."""
        await self._stop_autonomy_loop()

    async def stop(self) -> None:
        """停止自動化迴路。"""
        await self._stop_autonomy_loop()
        await super().stop()


__all__ = ["SystemRuntimeSovereign"]