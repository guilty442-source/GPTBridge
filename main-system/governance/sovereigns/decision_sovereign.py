"""Decision Sovereign — 決策主宰（最高決策權，啟動協調、維修決策、子主宰管理）。

法典依據:
- sovereign_id: decision-sovereign (position 2)
- area: decision
- rank: top-decision-sovereign
- basis: codex
- duties: startup-stack-dispatch|repair-decision-owner|sub-sovereign-owner|governance-rule-coordination
- powers: adjudicate-repair|dispatch-sub-sovereigns|coordinate-governance-rules
- prohibitions: FORBID:direct-execution|FORBID:own-health-decisions (A152/A154)

A63/A64 boundary: this sovereign is DECISION-ONLY. It does not materialize
or start sovereigns, sub-sovereigns, or services itself — the startup
dispatch is adjudicated here and EXECUTED by the governed executor
(``core_system.sovereign_stack_executor.SovereignStackExecutor``), which
materializes the child sub-sovereigns into this sovereign's registry and
performs the actual activation sequence.

Per A128/A130 (supersedes A63/A64), the mother process (GPTBridgeApp) must
not directly materialize or start sovereigns. Instead, it delegates the
sovereign stack startup to this sovereign via ``start_sovereign_stack``,
which adjudicates the dispatch and hands execution to the governed
``SovereignStackExecutor``.

The decision-sovereign also owns the repair DECISION chain per
A152/A154/E127/E128: the health-maintenance-test sub-sovereign classifies
health signals (health-only scope) and hands them to this sovereign, which
makes the repair decision, validates permissions, and routes to the
release-update (code change) or runtime-state (runtime action)
synchronization chain for governed execution.

Owned child sub-sovereigns (codex-aligned identities, A302–A323):
  * runtime-state-sync-sub-sovereign       -- keeps the platform running and serving
  * resource-dependency-sync-sub-sovereign -- owns all resource-body concerns
  * data-governance-sub-sovereign          -- owns all data-body concerns
  * channel-contract-sync-sub-sovereign    -- owns cross-sovereign structural interfaces
  * dependency-sync-sub-sovereign          -- third-party software management
  * learning-evidence-sync-sub-sovereign   -- persistent error learning
  * release-update-sync-sub-sovereign      -- governed code-change dispatch

All are LOCAL CODE (same process as GPTBridgeApp) and coordinate existing
in-process services; they never run heavy work in this mother process.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from governance_rule.execution.codex_official import official_self_declaration
from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from ._delegation import record_delegation_outcome
from core_system.codex_decision import accepted_outcome, refusal_outcome
from core_system.governance_rule_coordination import GovernanceRuleCoordination
from core_system.repair_decision_chain import RepairDecisionChain
from core_system.sovereign_utils import _iso_now

# Sub-modules
from .decision.child_access import DecisionChildAccessMixin
from .decision.startup_dispatch import DecisionStartupDispatchMixin
from .decision.repair_decision import DecisionRepairMixin
from .decision.certified_update import DecisionCertifiedUpdateMixin
from .decision.sub_sovereign import DecisionSubSovereignAssignMixin
from .decision.autonomy import DecisionAutonomyMixin
from .decision.status import DecisionStatusMixin
from .decision.state import DecisionStateMixin


# A74/A174: self-declaration through the official entry single-use
# session, not a direct codex snapshot import.
_DECISION_SOVEREIGN = official_self_declaration("decision-sovereign")
if _DECISION_SOVEREIGN is None:
    raise RuntimeError("decision sovereign not found in Governance Codex")

DECISION_SOVEREIGN_RESPONSIBILITIES = _DECISION_SOVEREIGN.duties

# A330 terminal statuses
_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "global-success",
    "failed-isolated",
    "rolled-back",
    "partial-deferred",
})

# Autonomous supervision cadence
_AUTONOMY_INTERVAL_SECONDS = 15.0
_CHILD_RESTART_BUDGET = 3
_CHILD_RESTART_COOLDOWN_SECONDS = 60.0


class DecisionSovereign(
    DecisionChildAccessMixin,
    DecisionStartupDispatchMixin,
    DecisionRepairMixin,
    DecisionCertifiedUpdateMixin,
    DecisionSubSovereignAssignMixin,
    DecisionAutonomyMixin,
    DecisionStatusMixin,
    DecisionStateMixin,
    SovereignBase,
):
    """決策主宰：啟動堆疊裁決/派工、維修決策、子主宰擁有者（不執行）。"""

    sovereign_id = "decision-sovereign"

    ROLE = _DECISION_SOVEREIGN.id

    # A10/A12 explicit intent allowlist
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        "startup.stack.dispatch",
        "repair.decide-and-route",
        "repair.certified-update",
        "sub-sovereign.assign",
        "governance-rule.coordinate",
    })

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        workspace_root = Path(getattr(app, "project_root", Path.cwd()))
        self.workspace_root = workspace_root.resolve()
        self.runtime_state_path = (
            self.workspace_root
            / "main-system"
            / "runtime"
            / "state"
            / "decision-sovereign.json"
        )
        self.launcher_report_path = (
            self.workspace_root
            / "main-system"
            / "launcher"
            / "state"
            / "orchestrator-report.json"
        )
        self.platform_id = "main-system"
        self.module_id = "decision-sovereign"
        # Child registry — populated by the governed executor at activation.
        self._sub_sovereigns: dict[str, Any] = {}
        self.governance_rule_coordination = GovernanceRuleCoordination(app)
        # A152/A154/E127/E128: repair decision chain
        self._repair_decision_chain = RepairDecisionChain(app)
        # A330 certified-update operation tracking
        self._certified_updates: dict[str, dict[str, Any]] = {}
        # Autonomous supervision
        self._autonomy_task: asyncio.Task[Any] | None = None
        self._autonomy_stop = asyncio.Event()
        self._child_supervision: dict[str, dict[str, Any]] = {}

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
        """裁決：啟動協調、維修路由、子主宰指派、法典協調。"""
        intent = request.intent

        if intent == "startup.stack.dispatch":
            return await self._adjudicate_startup_dispatch(request)
        if intent == "repair.decide-and-route":
            return await self._adjudicate_repair_decision(request)
        if intent == "repair.certified-update":
            return await self._adjudicate_certified_update(request)
        if intent == "sub-sovereign.assign":
            return await self._adjudicate_sub_sovereign_assign(request)
        if intent == "governance-rule.coordinate":
            return await self._adjudicate_governance_coordination(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A10", "A12"))

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """決策主宰委派執行（A69/A121）。

        This sovereign is decision-only (A297). Execution is dispatched
        inside ``_adjudicate`` through the governed repair decision chain
        and through ``delegate_to`` for A330 certified updates.  This hook
        attests that the adjudication was a pure decision and records the
        delegation outcome in the audit ledger.
        """
        return self._attach_delegation_receipt(decision, request, "decision-only")

    # ------------------------------------------------------------------
    # Lifecycle (decision-layer only; activation is executor work)
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Mark the Decision Sovereign active and surface its children.

        Decision-layer initialization only (A297): the supervision loop is
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
        if hasattr(self, "_stop_autonomy_loop"):
            await self._stop_autonomy_loop()


__all__ = ["DECISION_SOVEREIGN_RESPONSIBILITIES", "DecisionSovereign"]