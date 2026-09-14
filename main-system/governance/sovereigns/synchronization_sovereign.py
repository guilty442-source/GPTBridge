"""Synchronization Sovereign — 同步主宰（專門決策主宰，A330 認證更新執行例外）。

法典依據:
- sovereign_id: synchronization-sovereign (position 18)
- area: synchronization-decision
- rank: specialized-decision-sovereign-with-A330-certified-update-execution-exception
- basis: A301
- duties: resource-sync|channel-sync|release-sync|learning-sync|runtime-sync|repair-sync|cleanup-sync|log-sync
- powers: adjudicate-sync-decisions|A330-certified-update-execution
- prohibitions: FORBID:general-execution (except A330)

A301: synchronization policy+priority+consistency target+conflict
disposition+acceptance decision only.
A322: SOLE-DECISION over sync target + dependency order + atomic boundary
+ conflict isolation + retry/cancel + convergence acceptance.
A334: this sovereign is the codex-registered single parent of every
synchronization sub-sovereign.  Child identity -> primary domain and the
delegation target are resolved from ``sovereign_hierarchy_registry`` at
adjudication time; nothing here hard-codes the hierarchy.
A330: certified update-set execution exception — the only execution
power, and only after the certification proof adjudication passes.

Lifecycle boundary: the governed executor materializes and starts each
child ONLY after ``authorize_child_activation`` (or the dispatch wrapper
``dispatch_child_activation``) returns an accepted outcome.  The sovereign
adjudicates; the executor executes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import (
    accepted_outcome,
    refusal_outcome,
    verified_basis,
)

from ..registries import (
    children_of,
    module_assignment,
    parent_of,
    primary_domain_of,
    validate_child_parent,
)

from .synchronization.child_access import SyncChildAccessMixin
from .synchronization.sync_dispatch import SyncDispatchMixin
from .synchronization.child_lifecycle import SyncChildLifecycleMixin
from .synchronization.a330_execution import SyncA330ExecutionMixin
from .synchronization.a322_decision import SyncA322DecisionMixin
from .synchronization.autonomy import SyncAutonomyMixin
from .synchronization.status import SyncStatusMixin

_logger = logging.getLogger("gptbridge.sovereign.synchronization")

# Sync intent -> codex child identity (A334)
_SYNC_INTENT_CHILDREN: dict[str, str] = {
    "sync.resource-dependency": "resource-dependency-sync-sub-sovereign",
    "sync.channel-contract": "channel-contract-sync-sub-sovereign",
    "sync.release-update": "release-update-sync-sub-sovereign",
    "sync.learning-evidence": "learning-evidence-sync-sub-sovereign",
    "sync.runtime-state": "runtime-state-sync-sub-sovereign",
    "sync.repair-backup": "repair-backup-sync-sub-sovereign",
    "sync.cleanup-retention": "cleanup-retention-sync-sub-sovereign",
    "sync.automatic-log": "automatic-log-sync-sub-sovereign",
    "sync.dependency": "dependency-sync-sub-sovereign",
}

# A330: update types covered by the certified-update execution exception
_A330_UPDATE_TYPES: frozenset[str] = frozenset(
    {"backend-release", "codex", "governance-policy", "directory"}
)

# Bounded restart budget for child failure adjudication (A322 retry/cancel)
_MAX_CHILD_RESTARTS = 3


class SynchronizationSovereign(
    SovereignBase,
    SyncChildAccessMixin,
    SyncDispatchMixin,
    SyncChildLifecycleMixin,
    SyncA330ExecutionMixin,
    SyncA322DecisionMixin,
    SyncAutonomyMixin,
    SyncStatusMixin,
):
    """同步主宰：專門決策，協調各類同步子主宰，A330例外執行。"""

    sovereign_id = "synchronization-sovereign"

    # A10/A12 explicit intent allowlist
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        # Sync dispatch intents (A334)
        "sync.resource-dependency",
        "sync.channel-contract",
        "sync.release-update",
        "sync.learning-evidence",
        "sync.runtime-state",
        "sync.repair-backup",
        "sync.cleanup-retention",
        "sync.automatic-log",
        "sync.dependency",
        # Child lifecycle (A334)
        "sub-sovereign.activate",
        "sub-sovereign.deactivate",
        "sub-sovereign.report-failure",
        # Module routing (A334)
        "module.route",
        # A330 certified update execution (sole execution exception)
        "A330.certified-update",
        # A322 sync decision adjudication
        "sync.decision",
    })

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        # Child registry — populated by the governed executor at activation.
        self._sub_sovereigns: dict[str, Any] = {}
        # A330 certified update operations
        self._certified_update_operations: dict[str, dict[str, Any]] = {}
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
        """裁決：同步調度、子主宰生命週期、模組路由、A330執行、A322決策。"""
        intent = request.intent

        # Sync dispatch intents
        if intent in _SYNC_INTENT_CHILDREN:
            return await self._adjudicate_sync_dispatch(request)

        # Child lifecycle
        if intent == "sub-sovereign.activate":
            return await self._adjudicate_sub_sovereign_activate(request)
        if intent == "sub-sovereign.deactivate":
            return await self._adjudicate_sub_sovereign_deactivate(request)
        if intent == "sub-sovereign.report-failure":
            return await self._adjudicate_sub_sovereign_report_failure(request)

        # Module routing
        if intent == "module.route":
            return await self._adjudicate_module_routing(request)

        # A330 certified update execution
        if intent == "A330.certified-update":
            return await self._adjudicate_a330_certified_update(request)

        # A322 sync decision adjudication
        if intent == "sync.decision":
            return await self._adjudicate_sync_decision(request)

        return refusal_outcome("UNKNOWN_INTENT", verified_basis("A10", "A12"))

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """同步主宰委派執行（A69/A121）。

        This sovereign is decision-only except for A330 certified update
        execution.  The delegate_to calls inside _adjudicate already
        dispatch execution to the governed executor or sub-sovereigns.
        """
        return decision

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Mark the Synchronization Sovereign active."""
        state = await super().start()
        app_registry = getattr(self.app, "_sub_sovereigns", None)
        if isinstance(app_registry, dict):
            app_registry.update(self._sub_sovereigns)
        state["sub_sovereigns"] = list(self._sub_sovereigns.keys())
        self._start_autonomy_loop()
        return state

    async def stop(self) -> None:
        """Stop autonomous supervision."""
        await self._stop_autonomy_loop()
        await super().stop()


__all__ = ["SynchronizationSovereign"]