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

from .parallel_adjudication_mixin import ParallelAdjudicationMixin
from .decision.startup_dispatch import DecisionStartupDispatchMixin

# A74/A435: self-declaration through the official entry single-use
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

_COORDINATED_SUB_SOVEREIGN_IDS = (
    "runtime-state-sync-sub-sovereign",
    "resource-dependency-sync-sub-sovereign",
    "data-governance-sub-sovereign",
    "channel-contract-sync-sub-sovereign",
    "dependency-sync-sub-sovereign",
)

# Autonomous supervision cadence
_AUTONOMY_INTERVAL_SECONDS = 15.0
_CHILD_RESTART_BUDGET = 3
_CHILD_RESTART_COOLDOWN_SECONDS = 60.0


class DecisionSovereign(
    DecisionStartupDispatchMixin,
    ParallelAdjudicationMixin,
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
    # Parallel adjudication entry point
    # ------------------------------------------------------------------

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """並行裁決：啟動協調、維修路由、子主宰指派、法典協調。"""
        intent = request.intent

        # Parallel adjudication for independent intent groups
        handlers = {
            "startup.stack.dispatch": self._adjudicate_startup_dispatch,
            "repair.decide-and-route": self._adjudicate_repair_decision,
            "repair.certified-update": self._adjudicate_certified_update,
            "sub-sovereign.assign": self._adjudicate_sub_sovereign_assign,
            "governance-rule.coordinate": self._adjudicate_governance_coordination,
        }

        if intent in handlers:
            # Single intent: direct handler
            return await handlers[intent](request)

        # Unknown intent: fail-closed
        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A10", "A12"))

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """決策主宰委派執行（A446/A121）。

        This sovereign is decision-only (A297). Execution is dispatched
        inside ``_adjudicate`` through the governed repair decision chain
        and through ``delegate_to`` for A330 certified updates.  This hook
        attests that the adjudication was a pure decision and records the
        delegation outcome in the audit ledger.
        """
        return self._attach_delegation_receipt(decision, request, "decision-only")

    # ------------------------------------------------------------------
    # Adjudication handlers (modular, one per intent)
    # ------------------------------------------------------------------

    async def _adjudicate_startup_dispatch(self, request: SovereignRequest) -> SovereignOutcome:
        """A64: mother process delegates startup stack dispatch to decision-sovereign."""
        dependency_state = request.payload.get("dependency_state", "UNKNOWN")
        if dependency_state not in ("READY", "DEGRADED", "RECOVERY"):
            return refusal_outcome("INVALID_DEPENDENCY_STATE", self.verified_basis("A128", "A130"))

        return accepted_outcome(
            {
                "authorized": True,
                "sequence": [
                    "sync-sub-sovereigns-and-cleaner",
                    "permission-sovereign",
                    "maintenance-and-self-maintenance",
                    "decision-sovereign-and-sub-sovereigns",
                ],
                "dependency_state": dependency_state,
                "parallelism": "bounded-independent-per-A155",
            },
            self.verified_basis("A128", "A130", "A155"),
        )

    async def _adjudicate_repair_decision(self, request: SovereignRequest) -> SovereignOutcome:
        """A152/A154/E127/E128: repair decision chain."""
        classified_signal = request.payload.get("classified_signal")
        if not classified_signal:
            return refusal_outcome("MISSING_CLASSIFIED_SIGNAL", self.verified_basis("A152"))

        if not isinstance(classified_signal, dict):
            return refusal_outcome(
                "INVALID_CLASSIFIED_SIGNAL",
                self.verified_basis("A152", "A154"),
            )

        try:
            result = self._repair_decision_chain.decide_and_route(classified_signal)
        except Exception as error:
            return refusal_outcome(
                "REPAIR_CHAIN_ERROR",
                self.verified_basis("A152", "E128"),
            )

        if not isinstance(result, dict):
            return refusal_outcome(
                "REPAIR_CHAIN_INVALID_RESULT",
                self.verified_basis("A152"),
            )

        if not result.get("authorized", False):
            reason = result.get("reason", "REPAIR_NOT_AUTHORIZED")
            return refusal_outcome(
                reason,
                self.verified_basis("A152", "A154", "E128"),
            )

        return accepted_outcome(
            {
                "repair_decision": "authorized",
                "repair_type": classified_signal.get("repair_type", "unknown"),
                "route": "permission-validation > governed-executor > verification",
                "chain_result": result,
                "forbidden": "maintenance-owning-non-health-decisions",
            },
            self.verified_basis("A152", "A154", "E127", "E128"),
        )

    def decide_and_route_repair(
        self,
        classified_signal: dict[str, Any],
        *,
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """A152 repair-decision entry point."""
        return self._repair_decision_chain.decide_and_route(
            classified_signal, user_confirmed=user_confirmed
        )

    async def _adjudicate_certified_update(self, request: SovereignRequest) -> SovereignOutcome:
        """A152/A154/A330: certified update decision."""
        error, fields = self._validate_certified_update_request(request)
        if error is not None:
            return error
        update_type, update_set, artifact_hashes, operation_id = fields

        # Idempotent replay guard
        if operation_id in self._certified_updates:
            existing = self._certified_updates[operation_id]
            existing_status = existing.get("terminal_status", "")
            if existing_status in _TERMINAL_STATUSES:
                return accepted_outcome(
                    {
                        "repair_decision": "authorized",
                        "update_type": update_type,
                        "operation_id": operation_id,
                        "terminal_status": existing_status,
                        "idempotent_replay": True,
                    },
                    self.verified_basis("A152", "A154", "A330"),
                )
            return refusal_outcome(
                "OPERATION_IN_FLIGHT", self.verified_basis("A152", "A330")
            )

        # Delegate A330 execution to synchronization-sovereign
        return await self._delegate_certified_update(
            request, update_type, update_set, artifact_hashes, operation_id
        )

    def _validate_certified_update_request(
        self, request: SovereignRequest
    ) -> tuple[SovereignOutcome | None, tuple | None]:
        update_type = request.payload.get("update_type")
        if not update_type:
            return refusal_outcome(
                "MISSING_UPDATE_TYPE", self.verified_basis("A152", "A330")
            ), None
        if request.payload.get("certified") is not True:
            return refusal_outcome(
                "CERTIFICATION_MISSING", self.verified_basis("A152", "A330")
            ), None
        update_set = request.payload.get("update_set")
        if not isinstance(update_set, (list, tuple)) or not update_set:
            return refusal_outcome(
                "EMPTY_UPDATE_SET", self.verified_basis("A152", "A330")
            ), None
        artifact_hashes = request.payload.get("artifact_hashes")
        if not isinstance(artifact_hashes, dict) or not artifact_hashes:
            return refusal_outcome(
                "MISSING_ARTIFACT_HASHES", self.verified_basis("A152", "A330")
            ), None
        operation_id = str(request.payload.get("operation_id") or "")
        if not operation_id:
            return refusal_outcome(
                "MISSING_OPERATION_ID", self.verified_basis("A152", "A330")
            ), None
        return None, (update_type, update_set, artifact_hashes, operation_id)

    async def _delegate_certified_update(
        self,
        request: SovereignRequest,
        update_type: Any,
        update_set: Any,
        artifact_hashes: dict,
        operation_id: str,
    ) -> SovereignOutcome:
        sync_outcome = await self.delegate_to(
            "automation-sovereign",
            SovereignRequest(
                intent="A330.certified-update",
                subject=request.subject,
                requester=request.requester,
                payload=request.payload,
            ),
        )
        if not sync_outcome.accepted:
            if sync_outcome.refusal and sync_outcome.refusal.reason_code == (
                "TARGET_SOVEREIGN_UNAVAILABLE"
            ):
                return refusal_outcome(
                    "SYNCHRONIZATION_SOVEREIGN_UNAVAILABLE",
                    self.verified_basis("A152", "A330", "A301"),
                )
            return sync_outcome

        # Record for lifecycle tracking
        self._certified_updates[operation_id] = {
            "operation_id": operation_id,
            "update_type": update_type,
            "update_set": list(update_set),
            "artifact_hashes": dict(artifact_hashes),
            "target_generation": request.payload.get("target_generation", ""),
            "decision": "authorized",
            "sync_authorization": sync_outcome.result,
            "authorized_at": _iso_now(),
            "terminal_status": "authorized",
        }

        return accepted_outcome(
            {
                "repair_decision": "authorized",
                "update_type": update_type,
                "operation_id": operation_id,
                "route": "decision-sovereign > synchronization-sovereign(A330) > governed-executor",
                "sync_authorization": sync_outcome.result,
                "forbidden": "decision-sovereign-direct-execution",
            },
            self.verified_basis("A152", "A154", "A330", "A63", "A64"),
        )

    async def _adjudicate_sub_sovereign_assign(self, request: SovereignRequest) -> SovereignOutcome:
        """A64/A323: child sub-sovereign assignment."""
        sub_sovereign = request.payload.get("sub_sovereign")
        action = request.payload.get("action", "start")

        from governance.registries import children_of, parent_of

        if sub_sovereign not in children_of("decision-sovereign"):
            return refusal_outcome("UNKNOWN_SUB_SOVEREIGN", self.verified_basis("A130", "A334"))

        valid_actions = {"start", "stop", "coordinate", "assign", "status"}
        if action not in valid_actions:
            return refusal_outcome(
                "INVALID_ACTION",
                self.verified_basis("A10", "A130"),
            )

        return accepted_outcome(
            {
                "sub_sovereign": sub_sovereign,
                "action": action,
                "authority": f"parent-{parent_of(sub_sovereign)}",
                "execution": "delegated-to-governed-executor",
            },
            self.verified_basis("A130", "A284", "A287", "A323", "A334"),
        )

    async def _adjudicate_governance_coordination(self, request: SovereignRequest) -> SovereignOutcome:
        """Governance rule coordination (A63)."""
        edicts = self.edicts()
        children = sorted(self._sub_sovereigns.keys())
        return accepted_outcome(
            {
                "coordination": "governance-rules-aligned",
                "source": "codex-only",
                "edict_count": len(edicts),
                "children": children,
                "children_started": sum(
                    1
                    for c in self._sub_sovereigns.values()
                    if getattr(c, "started", False)
                ),
            },
            self.verified_basis("A12", "A128"),
        )

    def _child_status(self, child_id: str, method: str = "live_status") -> dict[str, Any]:
        child = getattr(self, "_sub_sovereigns", {}).get(child_id)
        if child is None:
            return {"role": child_id, "enabled": False, "materialized": False}
        reporter = getattr(child, method, None)
        return reporter() if callable(reporter) else {"role": child_id}

    def status(self) -> dict[str, Any]:
        from governance.registries import children_of

        state = self._load_state()
        maintenance_sovereign = getattr(self.app, "maintenance_sovereign", None)
        permission_sovereign = getattr(self.app, "permission_sovereign", None)
        return self._with_status_schema({
            "platform_id": self.platform_id,
            "module_id": self.module_id,
            "owned_by": self.module_id,
            "dependency_state": state.get("dependency_state", ""),
            "started_at": state.get("started_at", ""),
            "executor": "governed-executor-only",
            "sub_sovereigns": [
                self._child_status(child_id)
                for child_id in children_of("decision-sovereign")
            ],
            "coordinated_sub_sovereigns": self._coordinated_statuses("live_status"),
            "peer_systems": self._peer_statuses(),
            "health_owner": "health-maintenance-test-sub-sovereign",
            "governance_rules": self.governance_rule_coordination.coordination_status(),
            "certified_updates": self.certified_update_status(),
            "autonomy": self._autonomy_status(),
            "runtime-state-sync": self._child_status("runtime-state-sync-sub-sovereign"),
            "resource-dependency-sync": self._child_status("resource-dependency-sync-sub-sovereign"),
            "data-governance": self._child_status("data-governance-sub-sovereign"),
            "channel-contract-sync": self._child_status("channel-contract-sync-sub-sovereign"),
            "dependency-sync": self._child_status("dependency-sync-sub-sovereign"),
            "maintenance": (
                maintenance_sovereign.live_status()
                if maintenance_sovereign is not None
                else {"enabled": False}
            ),
            "permission": (
                permission_sovereign.coordination_status()
                if permission_sovereign is not None
                else {"enabled": False}
            ),
        })

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["sub_sovereign_registry"] = {
            name: sov.live_status() if hasattr(sov, "live_status") else {"role": name}
            for name, sov in self._all_children().items()
        }
        return base

    def orchestration_status(self) -> dict[str, Any]:
        """Unified subsystem health for the governing orchestrator."""
        from governance.registries import children_of

        maintenance_sovereign = getattr(self.app, "maintenance_sovereign", None)
        permission_sovereign = getattr(self.app, "permission_sovereign", None)
        return {
            "state": "delegated",
            "owner": self.module_id,
            "sub_sovereigns": [
                self._child_status(child_id, "orchestration_status")
                for child_id in children_of("decision-sovereign")
            ],
            "coordinated_sub_sovereigns": self._coordinated_statuses("orchestration_status"),
            "peer_systems": self._peer_statuses(),
            "health_owner": "health-maintenance-test-sub-sovereign",
            "governance_rules": self.governance_rule_coordination.orchestration_status(),
            "runtime-state-sync": self._child_status("runtime-state-sync-sub-sovereign", "orchestration_status"),
            "maintenance": (
                maintenance_sovereign.orchestration_status()
                if maintenance_sovereign is not None
                else {"enabled": False}
            ),
            "permission": (
                permission_sovereign.orchestration_status()
                if permission_sovereign is not None
                else {"enabled": False}
            ),
            "resource-dependency-sync": self._child_status("resource-dependency-sync-sub-sovereign", "orchestration_status"),
            "data-governance": self._child_status("data-governance-sub-sovereign", "orchestration_status"),
            "channel-contract-sync": self._child_status("channel-contract-sync-sub-sovereign", "orchestration_status"),
            "dependency-sync": self._child_status("dependency-sync-sub-sovereign", "orchestration_status"),
            "subsystems": self._subsystem_statuses(),
        }

    def _coordinated_statuses(self, method: str) -> list[dict[str, Any]]:
        return [
            self._child_status(child_id, method)
            for child_id in _COORDINATED_SUB_SOVEREIGN_IDS
        ]

    def _peer_statuses(self) -> dict[str, Any]:
        return {
            "learning": self._child_status(
                "learning-evidence-sync-sub-sovereign", "status"
            ),
            "programming": self._child_status(
                "release-update-sync-sub-sovereign", "status"
            ),
        }

    def _autonomy_status(self) -> dict[str, Any]:
        return {
            "enabled": self._autonomy_task is not None
            and not self._autonomy_task.done(),
            "supervised_children": len(self._child_supervision),
            "quarantined": [
                child_id
                for child_id, watch in self._child_supervision.items()
                if watch.get("quarantined")
            ],
        }

    def _subsystem_statuses(self) -> list[Any]:
        return [
            self.governance_rule_coordination.orchestration_status(),
            self._child_status("runtime-state-sync-sub-sovereign", "orchestration_status"),
            self._child_status("resource-dependency-sync-sub-sovereign", "orchestration_status"),
            self._child_status("data-governance-sub-sovereign", "orchestration_status"),
            self._child_status("channel-contract-sync-sub-sovereign", "orchestration_status"),
            self._child_status("dependency-sync-sub-sovereign", "orchestration_status"),
        ]

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
        await self._stop_autonomy_loop()

    # ------------------------------------------------------------------
    # Autonomous supervision (decision-layer only)
    # ------------------------------------------------------------------

    def _start_autonomy_loop(self) -> None:
        if self._autonomy_task is None or self._autonomy_task.done():
            self._autonomy_stop.clear()
            try:
                self._autonomy_task = asyncio.create_task(
                    self._autonomy_loop(),
                    name="decision-sovereign-autonomy",
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
                    timeout=_AUTONOMY_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _autonomy_tick(self) -> None:
        await self._supervise_children()
        self._reconcile_certified_updates()
        self._persist_live_state()

    async def _supervise_children(self) -> None:
        """Detect stopped children and adjudicate bounded restarts (A322)."""
        from governance.registries import parent_of, resolve_sovereign

        now = time.monotonic()
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
        if parent.child_failure_count(child_id) > _CHILD_RESTART_BUDGET:
            watch["quarantined"] = True
            watch["quarantined_at"] = _iso_now()
            return
        last_attempt = float(watch.get("last_attempt") or 0.0)
        if now - last_attempt < _CHILD_RESTART_COOLDOWN_SECONDS:
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

    def _reconcile_certified_updates(self) -> None:
        """Close A330 feedback loop when watcher missed a report."""
        active = {
            operation_id
            for operation_id, record in self._certified_updates.items()
            if record.get("terminal_status") not in _TERMINAL_STATUSES
        }
        if not active:
            return
        request_path = (
            self.workspace_root
            / "main-system"
            / "runtime"
            / "state"
            / "backend-update-request.json"
        )
        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        operation = (
            payload.get("last_update_operation")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(operation, dict):
            return
        operation_id = str(operation.get("operation_id") or "")
        status = str(operation.get("status") or "")
        if operation_id in active and status in _TERMINAL_STATUSES:
            self.record_certified_update_status(
                operation_id,
                status,
                reconciled_from="backend-update-request.json",
            )

    def _persist_live_state(self) -> None:
        """Keep decision-sovereign.json live between start and stop."""
        try:
            state = self._load_state()
            state.update(
                {
                    "sovereign": "decision-sovereign",
                    "started": self._started,
                    "heartbeat_at": _iso_now(),
                    "autonomy": {
                        "enabled": True,
                        "interval_seconds": _AUTONOMY_INTERVAL_SECONDS,
                        "supervised_children": {
                            child_id: dict(watch)
                            for child_id, watch in
                            self._child_supervision.items()
                        },
                        "pending_repair_requests":
                            self._pending_repair_requests(),
                        "certified_updates_active": sum(
                            1
                            for record in self._certified_updates.values()
                            if record.get("terminal_status")
                            not in _TERMINAL_STATUSES
                        ),
                    },
                }
            )
            self._save_state(state)
        except OSError:
            pass

    def _pending_repair_requests(self) -> int:
        from pathlib import Path
        import json
        path = (
            self.workspace_root
            / "main-system"
            / "runtime"
            / "state"
            / "repair-requests.json"
        )
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return 0
        requests = (
            payload.get("requests") if isinstance(payload, dict) else payload
        )
        if not isinstance(requests, list):
            return 0
        return sum(
            1
            for item in requests
            if isinstance(item, dict)
            and str(item.get("status") or "") == "pending"
        )

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.runtime_state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------------
    # Certified update status tracking
    # ------------------------------------------------------------------

    def record_certified_update_status(
        self, operation_id: str, terminal_status: str, **detail: Any
    ) -> bool:
        """Update tracked certified update's terminal status."""
        record = self._certified_updates.get(operation_id)
        if record is None:
            return False
        if terminal_status not in _TERMINAL_STATUSES:
            return False
        if record.get("terminal_status") in _TERMINAL_STATUSES:
            return False
        record["terminal_status"] = terminal_status
        record["updated_at"] = _iso_now()
        record.update(detail)
        return True

    def certified_update_status(self) -> dict[str, Any]:
        """Read-only lifecycle status of tracked operations."""
        active = [
            record
            for record in self._certified_updates.values()
            if record.get("terminal_status") not in _TERMINAL_STATUSES
        ]
        recent = [
            record
            for record in self._certified_updates.values()
            if record.get("terminal_status") in _TERMINAL_STATUSES
        ][-8:]
        return {
            "active_operations": active,
            "recent_terminal": recent,
            "total_tracked": len(self._certified_updates),
        }


__all__ = ["DECISION_SOVEREIGN_RESPONSIBILITIES", "DecisionSovereign"]