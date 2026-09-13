"""Decision Sovereign — 決策主宰（最高決策權，啟動協調、維修決策、子主宰管理）。

法典依據:
- sovereign_id: decision-sovereign (position 2)
- area: decision
- rank: top-decision-sovereign
- basis: codex
- duties: startup-stack-dispatch|repair-decision-owner|sub-sovereign-owner|governance-rule-coordination
- powers: adjudicate-repair|dispatch-sub-sovereigns|coordinate-governance-rules
- prohibitions: FORBID:direct-execution|FORBID:own-health-decisions (A152/A154)

A63/A64 boundary: this sovereign is DECISION-ONLY.  It does not materialize
or start sovereigns, sub-sovereigns, or services itself — the startup
dispatch is adjudicated here and EXECUTED by the governed executor
(``core_system.sovereign_stack_executor.SovereignStackExecutor``), which
materializes the child sub-sovereigns into this sovereign's registry and
performs the actual activation sequence.

The implementation was merged from the retired
``core_system.decision_sovereign.DecisionSovereignService`` so the active
startup path keeps its governed-executor dispatch behavior while operating
under the governance-layer sovereign identity.

The launcher (start.ps1) performs environment loading, runtime checks,
PostgreSQL/Qdrant/Ollama probing, and a governance audit BEFORE the Electron
and Python backend are launched.  That validated dependency state is handed to
the backend through two channels:

  * GPTBRIDGE_STARTUP_STATE            -- the READY/DEGRADED/RECOVERY string
  * <main-system>/launcher/state/orchestrator-report.json -- full service report

Per A128/A130 (supersedes A63/A64), the mother process (GPTBridgeApp) must
not directly materialize or start sovereigns.  Instead, it delegates the
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

from governance_rule.codex import GOVERNANCE_CODEX
from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome
from core_system.governance_rule_coordination import GovernanceRuleCoordination
from core_system.repair_decision_chain import RepairDecisionChain
from core_system.sovereign_utils import _iso_now


_DECISION_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.id == "decision-sovereign"),
    None,
)
if _DECISION_SOVEREIGN is None:
    raise RuntimeError("decision sovereign not found in Governance Codex")

DECISION_SOVEREIGN_RESPONSIBILITIES = _DECISION_SOVEREIGN.duties

# Legacy attribute names used by existing callers -> codex child identity
# (the codex ``sovereign_hierarchy_registry`` remains the sole authority
# for parent assignment per A334).
_CHILD_ATTRIBUTE_MAP: dict[str, str] = {
    "runtime_sovereign": "runtime-state-sync-sub-sovereign",
    "resource_sovereign": "resource-dependency-sync-sub-sovereign",
    "data_sovereign": "data-governance-sub-sovereign",
    "integration_sovereign": "channel-contract-sync-sub-sovereign",
    "third_party_sovereign": "dependency-sync-sub-sovereign",
    "learning_system_sovereign": "learning-evidence-sync-sub-sovereign",
    "system_programming_sovereign": "release-update-sync-sub-sovereign",
}

# A330 terminal statuses — used by certified_update_status() to
# distinguish active vs terminal operations.
_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "global-success",
    "failed-isolated",
    "rolled-back",
    "partial-deferred",
})

# Autonomous supervision cadence — the decision-sovereign continuously
# discharges its codex duties without external prodding: it detects
# children that stopped after activation, adjudicates bounded restarts
# (A322 budget on the codex parent's failure counter), reconciles
# certified-update lifecycle records, and keeps its persisted state live.
_AUTONOMY_INTERVAL_SECONDS = 15.0
_CHILD_RESTART_BUDGET = 3
_CHILD_RESTART_COOLDOWN_SECONDS = 60.0


class DecisionSovereign(SovereignBase):
    """決策主宰：啟動堆疊裁決/派工、維修決策、子主宰擁有者（不執行）。

    Sovereign-stack startup dispatcher (A64) and platform sub-sovereign
    owner.  Per A63 this sovereign holds decision power only; materialization
    and activation are executed by the governed ``SovereignStackExecutor``.
    """

    sovereign_id = "decision-sovereign"

    ROLE = _DECISION_SOVEREIGN.id

    # A10/A12 explicit intent allowlist — the codex edict IDs are article
    # tokens (A10, A12, ...), not the kebab-case intent strings used by
    # callers, so the base-class ``_verify_intent`` check against edict IDs
    # would reject every real intent.  This sovereign owns a fixed set of
    # decision intents; list them explicitly (fail-closed, A10/A11).
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
        # A152/A154/E127/E128: the decision-sovereign owns the repair
        # decision chain.  The health-maintenance sub-sovereign classifies
        # health signals (health-only) and delegates the decision here.
        self._repair_decision_chain = RepairDecisionChain(app)
        # A330 certified-update operation tracking — the decision-sovereign
        # records each certified update it authorizes so it can report the
        # operation lifecycle (prepared → authorized → executing →
        # converged/failed) without owning the execution itself.
        self._certified_updates: dict[str, dict[str, Any]] = {}
        # Autonomous supervision — started in start(), stopped in stop().
        self._autonomy_task: asyncio.Task[Any] | None = None
        self._autonomy_stop = asyncio.Event()
        # Per-child supervision watch: started/stopped transitions, restart
        # attempts and quarantine markers decided by this sovereign.
        self._child_supervision: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Child access (registry-backed; materialized by the governed executor)
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        child_id = _CHILD_ATTRIBUTE_MAP.get(name)
        if child_id is not None:
            return self._child(child_id)
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    @property
    def permission_sovereign(self) -> Any:
        """Read-only passthrough to the app's permission sovereign."""
        return getattr(self.app, "permission_sovereign", None)

    def _parent_for(self, child_id: str) -> Any | None:
        """A334: resolve a child identity to its codex-registered parent."""
        from governance.registries import parent_of, resolve_sovereign

        parent_id = parent_of(child_id)
        if parent_id == self.sovereign_id:
            return self
        return resolve_sovereign(self.app, parent_id)

    def _child(self, child_id: str) -> Any:
        """Resolve a child through its codex-registered parent's registry."""
        parent = self._parent_for(child_id)
        if parent is None:
            return None
        return getattr(parent, "_sub_sovereigns", {}).get(child_id)

    def _all_children(self) -> dict[str, Any]:
        """All materialized children across every parent's registry."""
        from governance.registries import hierarchy_status, resolve_sovereign

        merged: dict[str, Any] = {}
        for parent_id in hierarchy_status()["parents"]:
            parent = (
                self
                if parent_id == self.sovereign_id
                else resolve_sovereign(self.app, parent_id)
            )
            if parent is not None:
                merged.update(getattr(parent, "_sub_sovereigns", {}))
        return merged

    def _child_status(self, child_id: str, method: str = "live_status") -> dict[str, Any]:
        child = self._child(child_id)
        if child is None:
            return {"role": child_id, "enabled": False, "materialized": False}
        reporter = getattr(child, method, None)
        return reporter() if callable(reporter) else {"role": child_id}

    # ------------------------------------------------------------------
    # Intent gate (A10/A11 explicit allowlist)
    # ------------------------------------------------------------------

    def _verify_intent(self, intent: str) -> bool:
        """Override the base-class edict-ID check with this sovereign's
        explicit intent allowlist (A10/A11 fail-closed)."""
        return intent in self._INTENT_ALLOWLIST

    # ------------------------------------------------------------------
    # Single-gate adjudication (A10/A11)
    # ------------------------------------------------------------------

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：啟動協調、維修路由、子主宰指派。"""
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

    async def _adjudicate_startup_dispatch(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A64: 母進程委派啟動堆疊給決策主宰。"""
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

    async def _adjudicate_repair_decision(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A152/A154/E127/E128: 維修決策鏈。

        Routes the classified signal through the repair decision chain
        (permission validation → governed executor → verification).
        The decision-sovereign owns the decision; the chain executes
        the routing.  This method returns the chain's verdict — it does
        not execute the repair itself (A63/A64).
        """
        classified_signal = request.payload.get("classified_signal")
        if not classified_signal:
            return refusal_outcome("MISSING_CLASSIFIED_SIGNAL", self.verified_basis("A152"))

        if not isinstance(classified_signal, dict):
            return refusal_outcome(
                "INVALID_CLASSIFIED_SIGNAL",
                self.verified_basis("A152", "A154"),
            )

        # A152/E128: route through the repair decision chain.
        # The chain performs permission validation and dispatches to the
        # governed executor; the decision-sovereign owns the decision.
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
            # Permission denied or chain failed — return the chain's
            # verdict as a refusal so the caller sees the reason.
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

    async def _adjudicate_certified_update(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A152/A154/A330: 認證更新決策 — 決策主宰裁決後委派同步主宰執行。

        Per A152 the decision-sovereign owns the repair decision.  A
        certified backend-release update (A330) is a repair-class
        decision: the decision-sovereign validates the certification proof,
        authorizes the update, and delegates A330 execution to the
        synchronization-sovereign (the only sovereign with A330 execution
        power).  The decision-sovereign records the operation for lifecycle
        tracking but never executes the update itself (A63/A64).
        """
        # A330 certification gate — fail-closed (A10/A11).
        update_type = request.payload.get("update_type")
        if not update_type:
            return refusal_outcome(
                "MISSING_UPDATE_TYPE", self.verified_basis("A152", "A330")
            )
        if request.payload.get("certified") is not True:
            return refusal_outcome(
                "CERTIFICATION_MISSING", self.verified_basis("A152", "A330")
            )
        update_set = request.payload.get("update_set")
        if not isinstance(update_set, (list, tuple)) or not update_set:
            return refusal_outcome(
                "EMPTY_UPDATE_SET", self.verified_basis("A152", "A330")
            )
        artifact_hashes = request.payload.get("artifact_hashes")
        if not isinstance(artifact_hashes, dict) or not artifact_hashes:
            return refusal_outcome(
                "MISSING_ARTIFACT_HASHES", self.verified_basis("A152", "A330")
            )
        # A330: operation_id is required (one per operation, fail-closed).
        operation_id = str(request.payload.get("operation_id") or "")
        if not operation_id:
            return refusal_outcome(
                "MISSING_OPERATION_ID", self.verified_basis("A152", "A330")
            )
        # Reject duplicate operation-ids (idempotent replay guard).
        if operation_id in self._certified_updates:
            existing = self._certified_updates[operation_id]
            existing_status = existing.get("terminal_status", "")
            if existing_status in _TERMINAL_STATUSES:
                # Already terminal — return the recorded result (idempotent).
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
            # In-flight duplicate — refuse (concurrent coordinator guard).
            return refusal_outcome(
                "OPERATION_IN_FLIGHT", self.verified_basis("A152", "A330")
            )

        # Delegate A330 execution adjudication to the synchronization
        # sovereign — the sole holder of A330 execution power (A301/A322) —
        # through its single entry gate so the requester/intent checks run
        # instead of bypassing them with a private method call.
        sync_outcome = await self.delegate_to(
            "synchronization-sovereign",
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

        # Record the operation for lifecycle tracking.  The decision-sovereign
        # tracks the decision state; execution state is owned by boot_core
        # and the synchronization-sovereign.
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

    async def _adjudicate_sub_sovereign_assign(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A64/A323: 子主宰指派。"""
        sub_sovereign = request.payload.get("sub_sovereign")
        action = request.payload.get("action", "start")

        from governance.registries import children_of, parent_of

        if sub_sovereign not in children_of("decision-sovereign"):
            return refusal_outcome("UNKNOWN_SUB_SOVEREIGN", self.verified_basis("A130", "A334"))

        # A10: validate the action against the explicit allowlist.
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

    async def _adjudicate_governance_coordination(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """法典規則協調（A63）— 回報管轄敕令覆蓋與子層對齊狀態。"""
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

    def register_sub_sovereign(self, name: str, sovereign: Any) -> None:
        self._sub_sovereigns[name] = sovereign

    def get_sub_sovereign(self, name: str) -> Any | None:
        return self._sub_sovereigns.get(name)

    # ------------------------------------------------------------------
    # Lifecycle (decision-layer only; activation is executor work)
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Mark the Decision Sovereign active and surface its children.

        Materialization/activation of the stack is performed by the
        governed executor via ``start_sovereign_stack``; this activation is
        the decision-layer surface only.
        """
        state = await super().start()
        app_registry = getattr(self.app, "_sub_sovereigns", None)
        if isinstance(app_registry, dict):
            app_registry.update(self._sub_sovereigns)
        state["sub_sovereigns"] = list(self._sub_sovereigns.keys())
        self._start_autonomy_loop()
        return state

    async def start_sovereign_stack(self) -> bool:
        """Adjudicate the sovereign-stack startup dispatch, then delegate
        execution to the governed ``SovereignStackExecutor`` (A63/A64).

        This method does NOT materialize or start anything itself — it
        decides (A10/A11 fail-closed) and hands the authorized sequence to
        the governed executor, which performs all activation work.
        """
        outcome = await self._adjudicate_startup_dispatch(
            SovereignRequest(
                intent="startup.stack.dispatch",
                subject="sovereign-stack",
                requester="startup-executor",
                payload={"dependency_state": self._dependency_state()},
            )
        )
        if not outcome.accepted:
            reason = outcome.refusal.reason_code if outcome.refusal else "UNKNOWN"
            raise RuntimeError(f"startup-dispatch-refused:{reason}")

        executor = getattr(self.app, "sovereign_stack_executor", None)
        if executor is None:
            from core_system.sovereign_stack_executor import (
                SovereignStackExecutor,
            )

            executor = SovereignStackExecutor(self.app)
            self.app.sovereign_stack_executor = executor
        return await executor.activate(self)

    async def stop(self) -> None:
        """Dispatch deactivation to the governed executor, then stop."""
        await self._stop_autonomy_loop()
        executor = getattr(self.app, "sovereign_stack_executor", None)
        if executor is not None:
            await executor.deactivate(self)
        self._save_state({"stopped_at": _iso_now()})
        await super().stop()

    # ------------------------------------------------------------------
    # Autonomous supervision (decision-layer duties, no direct execution)
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
                # No running loop — supervision stays off; lifecycle and
                # handle() entry points remain fully functional.
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
            except Exception:
                pass  # Supervision never crashes the sovereign.
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
        """Detect stopped children and adjudicate bounded restarts.

        Decision-layer only: the consecutive-failure counter lives on the
        codex parent (A322), the restart/quarantine decision is made here,
        and the activation itself is handed to the governed executor
        (A63/A64).  Only materialized children are supervised —
        unmaterialized registry entries are the executor's startup concern.
        """
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
                # started -> stopped transition: feed the codex parent's
                # consecutive-failure counter (A322).
                parent_id = parent_of(child_id)
                parent = (
                    self
                    if parent_id == self.sovereign_id
                    else resolve_sovereign(self.app, parent_id)
                )
                if parent is not None:
                    try:
                        parent.record_child_failure(child_id)
                    except Exception:
                        pass
                watch["state"] = "stopped"
                watch["stopped_at"] = _iso_now()

            if watch.get("quarantined"):
                continue
            parent_id = parent_of(child_id)
            parent = (
                self
                if parent_id == self.sovereign_id
                else resolve_sovereign(self.app, parent_id)
            )
            if parent is None:
                continue
            if (
                parent.child_failure_count(child_id)
                > _CHILD_RESTART_BUDGET
            ):
                watch["quarantined"] = True
                watch["quarantined_at"] = _iso_now()
                continue
            last_attempt = float(watch.get("last_attempt") or 0.0)
            if now - last_attempt < _CHILD_RESTART_COOLDOWN_SECONDS:
                continue
            executor = getattr(self.app, "sovereign_stack_executor", None)
            if executor is None:
                continue
            watch["last_attempt"] = now
            watch["restart_attempts"] = (
                int(watch.get("restart_attempts") or 0) + 1
            )
            try:
                watch["last_result"] = await executor.restart_child(
                    self, child_id
                )
            except Exception as error:
                watch["last_result"] = {
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }

    def _reconcile_certified_updates(self) -> None:
        """Close the A330 feedback loop when the watcher missed a report."""
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

    def _pending_repair_requests(self) -> int:
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

    def _persist_live_state(self) -> None:
        """Keep decision-sovereign.json live between start and stop.

        Merges into the existing document so the executor's startup
        report (``startup_failures`` etc.) is preserved.
        """
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

    # ------------------------------------------------------------------
    # Repair decision (A152/A154/E127/E128)
    # ------------------------------------------------------------------

    def decide_and_route_repair(self, classified_signal: dict[str, Any]) -> dict[str, Any]:
        """A152 repair-decision entry point for the decision-sovereign.

        Per A152 (supersedes A67): ``REPAIR-DECISION:decision-sovereign``
        and ``FORBID:maintenance-owning-non-health-decisions``.  The
        health-maintenance-test sub-sovereign classifies the health signal
        (health-only scope, A154) and delegates the repair DECISION here.
        This method routes the classified signal through permission
        validation and governed execution (E128):
        ``decision > permission > runtime-or-release-update > executor
        > verification``.
        """
        return self._repair_decision_chain.decide_and_route(classified_signal)

    # ------------------------------------------------------------------
    # Certified-update lifecycle tracking (A152/A330 decision-layer)
    # ------------------------------------------------------------------

    def record_certified_update_status(
        self, operation_id: str, terminal_status: str, **detail: Any
    ) -> bool:
        """Update a tracked certified-update operation's terminal status.

        Called by the governed executor or synchronization-sovereign when
        a certified update reaches a terminal state (global-success,
        failed-isolated, rolled-back, partial-deferred).  The
        decision-sovereign records the outcome for lifecycle reporting
        but does not execute anything.  Returns True if the operation
        was found and updated; False if the operation_id is unknown or
        the terminal_status is invalid (fail-closed, A11).
        """
        record = self._certified_updates.get(operation_id)
        if record is None:
            return False
        if terminal_status not in _TERMINAL_STATUSES:
            return False
        if record.get("terminal_status") in _TERMINAL_STATUSES:
            # Already terminal — idempotent replay, do not overwrite.
            return False
        record["terminal_status"] = terminal_status
        record["updated_at"] = _iso_now()
        record.update(detail)
        return True

    def certified_update_status(self) -> dict[str, Any]:
        """Read-only lifecycle status of tracked certified-update operations."""
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

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        from governance.registries import children_of

        state = self._load_state()
        maintenance_sovereign = getattr(self.app, "maintenance_sovereign", None)
        permission_sovereign = getattr(self.app, "permission_sovereign", None)
        return {
            "sovereign": "decision-sovereign",
            "platform_id": self.platform_id,
            "module_id": self.module_id,
            "owned_by": self.module_id,
            "dependency_state": state.get("dependency_state", ""),
            "started_at": state.get("started_at", ""),
            "executor": "governed-executor-only",
            # A334: this list is this sovereign's own codex children;
            # sub-sovereigns coordinated under other parents are surfaced
            # through the named keys below (runtime/resource/data/...).
            "sub_sovereigns": [
                self._child_status(child_id)
                for child_id in children_of("decision-sovereign")
            ],
            "coordinated_sub_sovereigns": [
                self._child_status("runtime-state-sync-sub-sovereign"),
                self._child_status("resource-dependency-sync-sub-sovereign"),
                self._child_status("data-governance-sub-sovereign"),
                self._child_status("channel-contract-sync-sub-sovereign"),
                self._child_status("dependency-sync-sub-sovereign"),
            ],
            "peer_systems": {
                "learning": self._child_status(
                    "learning-evidence-sync-sub-sovereign", "status"
                ),
                "programming": self._child_status(
                    "release-update-sync-sub-sovereign", "status"
                ),
            },
            "health_owner": "health-maintenance-test-sub-sovereign",
            "governance_rules": self.governance_rule_coordination.coordination_status(),
            "certified_updates": self.certified_update_status(),
            "autonomy": {
                "enabled": self._autonomy_task is not None
                and not self._autonomy_task.done(),
                "supervised_children": len(self._child_supervision),
                "quarantined": [
                    child_id
                    for child_id, watch in self._child_supervision.items()
                    if watch.get("quarantined")
                ],
            },
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
        }

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["sub_sovereign_registry"] = {
            name: sov.live_status() if hasattr(sov, "live_status") else {"role": name}
            for name, sov in self._all_children().items()
        }
        return base

    def orchestration_status(self) -> dict[str, Any]:
        """Snap the governing orchestrator's unified subsystem health.

        The Xingcheng auxiliary system lives in the local-model governed
        executor process, keeping the heavy AI/model runtime isolated from the
        mother process (consistent with execution_delegation =
        governed-executor-only).  The sovereign coordinates it and surfaces the
        read-only governance rule authority, it does not import the heavy stack
        in-process and never mutates governance.  System health determination is
        owned by the health-maintenance sub-sovereign, not by this top sovereign.
        """
        from governance.registries import children_of

        maintenance_sovereign = getattr(self.app, "maintenance_sovereign", None)
        permission_sovereign = getattr(self.app, "permission_sovereign", None)
        return {
            "state": "delegated",
            "owner": self.module_id,
            # A334: own codex children; coordinated cross-parent children are
            # surfaced through the named keys and ``subsystems`` below.
            "sub_sovereigns": [
                self._child_status(child_id, "orchestration_status")
                for child_id in children_of("decision-sovereign")
            ],
            "coordinated_sub_sovereigns": [
                self._child_status(
                    "runtime-state-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "resource-dependency-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "data-governance-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "channel-contract-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "dependency-sync-sub-sovereign", "orchestration_status"
                ),
            ],
            "peer_systems": {
                "learning": self._child_status(
                    "learning-evidence-sync-sub-sovereign", "status"
                ),
                "programming": self._child_status(
                    "release-update-sync-sub-sovereign", "status"
                ),
            },
            "health_owner": "health-maintenance-test-sub-sovereign",
            "governance_rules": self.governance_rule_coordination.orchestration_status(),
            "runtime-state-sync": self._child_status(
                "runtime-state-sync-sub-sovereign", "orchestration_status"
            ),
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
            "resource-dependency-sync": self._child_status(
                "resource-dependency-sync-sub-sovereign", "orchestration_status"
            ),
            "data-governance": self._child_status(
                "data-governance-sub-sovereign", "orchestration_status"
            ),
            "channel-contract-sync": self._child_status(
                "channel-contract-sync-sub-sovereign", "orchestration_status"
            ),
            "dependency-sync": self._child_status(
                "dependency-sync-sub-sovereign", "orchestration_status"
            ),
            "subsystems": [
                self.governance_rule_coordination.orchestration_status(),
                self._child_status(
                    "runtime-state-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "resource-dependency-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "data-governance-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "channel-contract-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "dependency-sync-sub-sovereign", "orchestration_status"
                ),
            ],
        }

    # ------------------------------------------------------------------
    # Dependency state source
    # ------------------------------------------------------------------

    def _dependency_state(self) -> str:
        env_state = str(os.environ.get("GPTBRIDGE_STARTUP_STATE", "")).strip()
        if env_state:
            return env_state
        report = self._load_report()
        return str(report.get("state") or "UNKNOWN")

    def _load_report(self) -> dict[str, Any]:
        try:
            payload = json.loads(
                self.launcher_report_path.read_text(encoding="utf-8")
            )
            return payload if isinstance(payload, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.runtime_state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _save_state(self, payload: dict[str, Any]) -> None:
        self.runtime_state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.runtime_state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.runtime_state_path)


__all__ = ["DECISION_SOVEREIGN_RESPONSIBILITIES", "DecisionSovereign"]
