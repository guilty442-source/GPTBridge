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
from ._delegation import record_delegation_outcome
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

from .parallel_adjudication_mixin import ParallelAdjudicationMixin

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
    ParallelAdjudicationMixin,
    SovereignBase,
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
    # Parallel adjudication entry point
    # ------------------------------------------------------------------

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """並行裁決：同步調度、子主宰生命週期、模組路由、A330執行、A322決策。"""
        intent = request.intent

        # Sync dispatch intents - parallel adjudication for independent sync domains
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

        return refusal_outcome("UNKNOWN_INTENT", verified_basis(("A10", "A12")))

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """同步主宰委派執行（A446/A121）。

        This sovereign is decision-only except for A330 certified update
        execution.  The delegate_to calls inside _adjudicate already
        dispatch execution to the governed executor or sub-sovereigns.
        This hook attests that and records the delegation outcome in the
        audit ledger.
        """
        execution_mode = "A330-certified-update" if request.intent == "A330.certified-update" else "decision-only"
        return self._attach_delegation_receipt(decision, request, execution_mode)

    # ------------------------------------------------------------------
    # Adjudication handlers
    # ------------------------------------------------------------------

    async def _adjudicate_sync_dispatch(self, request: SovereignRequest) -> SovereignOutcome:
        """A301: adjudicate sync coordination intent."""
        child_id, child = self._resolve_sync_child(request.intent)
        if child_id is None or child is None:
            return refusal_outcome(
                "UNKNOWN_SYNC_INTENT", verified_basis(("A301", "A334"))
            )

        # Delegate to the sub-sovereign through the governed executor
        # (this sovereign adjudicates; executor executes)
        return await self.delegate_to(
            child_id,
            SovereignRequest(
                intent=request.intent,
                subject=request.subject,
                requester=request.requester,
                payload=request.payload,
            ),
        )

    def _resolve_sync_child(self, intent: str) -> tuple[str | None, Any | None]:
        child_id = _SYNC_INTENT_CHILDREN.get(intent)
        if not child_id:
            return None, None
        child = self._sub_sovereigns.get(child_id)
        return child_id, child

    async def _adjudicate_module_routing(self, request: SovereignRequest) -> SovereignOutcome:
        """Route module-level operations to assigned sub-sovereign (A334)."""
        module = request.payload.get("module")
        if not module:
            return refusal_outcome("MISSING_MODULE", verified_basis(("A334",)))

        assignment = module_assignment(module)
        if not assignment:
            return refusal_outcome("MODULE_UNASSIGNED", verified_basis(("A334",)))

        child_id = str(
            assignment.get("managing_sub_sovereign")
            or assignment.get("sub_sovereign")
            or ""
        )
        if not child_id:
            return refusal_outcome(
                "SUB_SOVEREIGN_UNASSIGNED", verified_basis(("A334",))
            )
        if child_id not in self._sub_sovereigns:
            return refusal_outcome(
                "SUB_SOVEREIGN_NOT_MATERIALIZED", verified_basis(("A334",))
            )

        return await self.delegate_to(
            child_id,
            SovereignRequest(
                intent=request.intent,
                subject=request.subject,
                requester=request.requester,
                payload=request.payload,
            ),
        )

    async def _adjudicate_sub_sovereign_activate(self, request: SovereignRequest) -> SovereignOutcome:
        """Activate a sub-sovereign through governed executor."""
        child_id = request.payload.get("child_id")
        if not child_id or child_id not in children_of(self.sovereign_id):
            return refusal_outcome("INVALID_CHILD_ID", verified_basis(("A334",)))
        return accepted_outcome(
            {"child_id": child_id, "action": "activate", "execution": "governed-executor"},
            verified_basis(("A334", "A301")),
        )

    async def _adjudicate_sub_sovereign_deactivate(self, request: SovereignRequest) -> SovereignOutcome:
        """Deactivate a sub-sovereign through governed executor."""
        child_id = request.payload.get("child_id")
        if not child_id or child_id not in children_of(self.sovereign_id):
            return refusal_outcome("INVALID_CHILD_ID", verified_basis(("A334",)))
        return accepted_outcome(
            {"child_id": child_id, "action": "deactivate", "execution": "governed-executor"},
            verified_basis(("A334", "A301")),
        )

    async def _adjudicate_sub_sovereign_report_failure(self, request: SovereignRequest) -> SovereignOutcome:
        """Handle child failure report (A322)."""
        child_id = request.payload.get("child_id")
        error = request.payload.get("error", "unknown")
        if not child_id or child_id not in children_of(self.sovereign_id):
            return refusal_outcome("INVALID_CHILD_ID", verified_basis(("A334",)))

        # Record failure and adjudicate restart per A322
        self.record_child_failure(child_id)
        return accepted_outcome(
            {
                "child_id": child_id,
                "failure_recorded": True,
                "error": error,
                "restart_adjudication": "bounded-per-A322",
            },
            verified_basis(("A322", "A334")),
        )

    async def _adjudicate_a330_certified_update(self, request: SovereignRequest) -> SovereignOutcome:
        """A330: certified update execution (sole execution exception)."""
        # A330/A152/A154: the sole execution exception may only be reached
        # through the decision layer — certification is adjudicated by
        # decision-sovereign, which delegates here with a verified
        # single-use nonce. A direct caller (even a governed actor) cannot
        # self-declare certification; the payload flag alone is not proof.
        error, fields = self._validate_a330_request(request)
        if error is not None:
            return error
        update_type, update_set, artifact_hashes, operation_id = fields

        # Idempotent replay guard
        if operation_id in self._certified_update_operations:
            existing = self._certified_update_operations[operation_id]
            existing_status = existing.get("terminal_status", "")
            if existing_status in ("global-success", "failed-isolated", "rolled-back", "partial-deferred"):
                return accepted_outcome(
                    {
                        "repair_decision": "authorized",
                        "update_type": update_type,
                        "operation_id": operation_id,
                        "terminal_status": existing_status,
                        "idempotent_replay": True,
                    },
                    verified_basis(("A330",)),
                )
            return refusal_outcome("OPERATION_IN_FLIGHT", verified_basis(("A330",)))

        # Execute via governed executor (A330 exception)
        executor = getattr(self.app, "governed_executor", None)
        if executor is None:
            return refusal_outcome(
                "GOVERNED_EXECUTOR_UNAVAILABLE", verified_basis(("A330", "A69"))
            )

        # Record operation
        self._certified_update_operations[operation_id] = {
            "operation_id": operation_id,
            "update_type": update_type,
            "update_set": list(update_set),
            "artifact_hashes": dict(artifact_hashes),
            "target_generation": request.payload.get("target_generation", ""),
            "started_at": _iso_now(),
            "status": "executing",
        }

        return accepted_outcome(
            {
                "operation_id": operation_id,
                "update_type": update_type,
                "execution": "A330-certified-update-exception",
                "executor": "governed-executor",
            },
            verified_basis(("A330", "A301", "A446")),
        )

    def _validate_a330_request(
        self, request: SovereignRequest
    ) -> tuple[SovereignOutcome | None, tuple | None]:
        """Validate A330 certified update request (A330/A152/A154)."""
        verified_delegation = request.payload.get("_verified_delegation")
        if not isinstance(verified_delegation, dict) or (
            verified_delegation.get("parent") != "decision-sovereign"
        ):
            return refusal_outcome(
                "CERTIFICATION_AUTHORITY_MISSING",
                verified_basis(("A330", "A152", "A154")),
            ), None

        update_type = request.payload.get("update_type")
        if not update_type:
            return refusal_outcome("MISSING_UPDATE_TYPE", verified_basis(("A330",))), None

        if update_type not in _A330_UPDATE_TYPES:
            return refusal_outcome("INVALID_A330_UPDATE_TYPE", verified_basis(("A330",))), None

        if request.payload.get("certified") is not True:
            return refusal_outcome("CERTIFICATION_MISSING", verified_basis(("A330",))), None

        update_set = request.payload.get("update_set")
        if not isinstance(update_set, (list, tuple)) or not update_set:
            return refusal_outcome("EMPTY_UPDATE_SET", verified_basis(("A330",))), None

        artifact_hashes = request.payload.get("artifact_hashes")
        if not isinstance(artifact_hashes, dict) or not artifact_hashes:
            return refusal_outcome("MISSING_ARTIFACT_HASHES", verified_basis(("A330",))), None

        operation_id = str(request.payload.get("operation_id") or "")
        if not operation_id:
            return refusal_outcome("MISSING_OPERATION_ID", verified_basis(("A330",))), None

        return None, (update_type, update_set, artifact_hashes, operation_id)

    async def _adjudicate_sync_decision(self, request: SovereignRequest) -> SovereignOutcome:
        """A322: sync decision adjudication."""
        decision_type = request.payload.get("decision_type")
        target = request.payload.get("target")
        return accepted_outcome(
            {
                "decision_type": decision_type,
                "target": target,
                "authority": "synchronization-sovereign",
                "basis": "A322",
            },
            verified_basis(("A322", "A301", "A334")),
        )

    def _child_status(self, child_id: str, method: str = "live_status") -> dict[str, Any]:
        child = getattr(self, "_sub_sovereigns", {}).get(child_id)
        if child is None:
            return {"role": child_id, "enabled": False, "materialized": False}
        reporter = getattr(child, method, None)
        return reporter() if callable(reporter) else {"role": child_id}

    def status(self) -> dict[str, Any]:
        from governance.registries import children_of

        return self._with_status_schema({
            "sovereign": self.sovereign_id,
            "sub_sovereigns": [
                self._child_status(child_id)
                for child_id in children_of(self.sovereign_id)
            ],
            "certified_updates": {
                "active": sum(
                    1 for op in self._certified_update_operations.values()
                    if op.get("status") not in ("completed", "failed")
                ),
                "total": len(self._certified_update_operations),
            },
            "autonomy": {
                "enabled": self._autonomy_task is not None
                and not self._autonomy_task.done(),
                "supervised_children": len(self._child_supervision),
            },
        })

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

    # ------------------------------------------------------------------
    # Autonomous supervision
    # ------------------------------------------------------------------

    def _start_autonomy_loop(self) -> None:
        if self._autonomy_task is None or self._autonomy_task.done():
            self._autonomy_stop.clear()
            try:
                self._autonomy_task = asyncio.create_task(
                    self._autonomy_loop(),
                    name="synchronization-sovereign-autonomy",
                )
            except RuntimeError:
                self._autonomy_task = None

    async def _stop_autonomy_loop(self) -> None:
        task = self._autonomy_task
        self._autonomy_task = None
        self._autonomy_stop.set()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _autonomy_loop(self) -> None:
        while not self._autonomy_stop.is_set():
            try:
                await self._autonomy_tick()
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
                pass
            try:
                await asyncio.wait_for(
                    self._autonomy_stop.wait(),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _autonomy_tick(self) -> None:
        await self._supervise_children()
        self._persist_live_state()

    async def _supervise_children(self) -> None:
        """Detect stopped children and adjudicate bounded restarts (A322)."""
        from governance.registries import parent_of, resolve_sovereign

        now = asyncio.get_event_loop().time()
        for child_id, child in self._all_children().items():
            if bool(getattr(child, "_started", False)):
                watch = self._child_supervision.get(child_id)
                if watch is not None and watch.get("state") != "started":
                    watch["state"] = "started"
                    watch["recovered_at"] = _iso_now()
                    watch.pop("quarantined", None)
                continue

            watch = self._child_supervision.setdefault(
                child_id, {"state": "started", "restart_attempts": 0}
            )
            if watch.get("state") == "started":
                parent_id = parent_of(child_id)
                parent = (
                    self
                    if parent_id == self.sovereign_id
                    else resolve_sovereign(self.app, parent_id)
                )
                if parent is not None:
                    try:
                        parent.record_child_failure(child_id)
                    except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
                        pass
                watch["state"] = "stopped"
                watch["stopped_at"] = _iso_now()

            await self._attempt_child_restart(child_id, watch, now)

    async def _attempt_child_restart(
        self, child_id: str, watch: dict, now: float
    ) -> None:
        from governance.registries import parent_of, resolve_sovereign

        if watch.get("quarantined"):
            return
        parent_id = parent_of(child_id)
        parent = (
            self
            if parent_id == self.sovereign_id
            else resolve_sovereign(self.app, parent_id)
        )
        if parent is None:
            return
        if parent.child_failure_count(child_id) > _MAX_CHILD_RESTARTS:
            watch["quarantined"] = True
            watch["quarantined_at"] = _iso_now()
            return
        last_attempt = float(watch.get("last_attempt") or 0.0)
        if now - last_attempt < 60.0:
            return
        executor = getattr(self.app, "sovereign_stack_executor", None)
        if executor is None:
            return
        watch["last_attempt"] = now
        watch["restart_attempts"] = int(watch.get("restart_attempts") or 0) + 1
        try:
            watch["last_result"] = await executor.restart_child(self, child_id)
        except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError) as error:
            watch["last_result"] = {
                "ok": False,
                "error": f"{type(error).__name__}: {error}",
            }

    def _persist_live_state(self) -> None:
        """Keep live state for monitoring."""
        try:
            from pathlib import Path
            import json
            state_path = (
                Path(getattr(self.app, "project_root", Path.cwd()))
                / "main-system"
                / "runtime"
                / "state"
                / "synchronization-sovereign.json"
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "sovereign": "synchronization-sovereign",
                "started": self._started,
                "heartbeat_at": _iso_now(),
                "autonomy": {
                    "enabled": True,
                    "supervised_children": {
                        child_id: dict(watch)
                        for child_id, watch in self._child_supervision.items()
                    },
                    "certified_updates_active": sum(
                        1
                        for record in self._certified_update_operations.values()
                        if record.get("status") not in ("completed", "failed")
                    ),
                },
            }
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            import os
            os.replace(temporary, state_path)
        except OSError:
            pass


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


__all__ = ["SynchronizationSovereign"]