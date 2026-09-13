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
from core_system.codex_decision import accepted_outcome, refusal_outcome

from ..registries import (
    children_of,
    primary_domain_of,
    validate_child_parent,
)

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


class SystemRuntimeSovereign(SovereignBase):
    """系統運行主宰：進程存活、運行完整性、平台服務決策。"""

    sovereign_id = "system-runtime-sovereign"

    # A10/A11 explicit intent allowlist — the base-class ``_verify_intent``
    # checks edict IDs (article tokens like A28), not the kebab-case intent
    # strings used by callers, so every real intent would be rejected and
    # ``handle()`` would be unreachable.  List adjudicated intents
    # explicitly (fail-closed).
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
            "convergence_accepts": 0,
            "conflict_isolations": 0,
            "last_auto_cycle": "",
        }
        # Isolated children pending re-acceptance (A322 conflict isolation).
        self._isolated_children: set[str] = set()
        # Last known convergence state per child.
        self._child_convergence: dict[str, str] = {}
        # Last runtime readiness state (for transition detection).
        self._last_readiness_state: str = ""

    def _verify_intent(self, intent: str) -> bool:
        """A10/A11 fail-closed: only declared runtime intents pass."""
        return intent in self._INTENT_ALLOWLIST

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
        if intent == "sub-sovereign.activate":
            return self._adjudicate_child_activation(request)
        if intent == "sub-sovereign.deactivate":
            return self._adjudicate_child_deactivation(request)
        if intent == "sub-sovereign.report-failure":
            return self._adjudicate_child_failure(request)
        if intent == "module.route":
            return self._adjudicate_module_route(request)
        if intent == "runtime.retry-cancel":
            return self._adjudicate_retry_cancel(request)
        if intent == "runtime.convergence-acceptance":
            return self._adjudicate_convergence(request)
        if intent == "runtime.conflict-isolation":
            return self._adjudicate_conflict_isolation(request)

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
            self.verified_basis("A28", "A128", "A130"),
        )

    async def _adjudicate_sub_sovereign_manage(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A284/A287/A334: 系統模組管理/指派/協調（子主宰）。

        The delegation target is resolved from the payload and validated
        against the codex hierarchy registry — this sovereign is the
        registered parent of ``system-sub-sovereign`` and
        ``startup-sub-sovereign``; anything else fails closed.
        """
        child_id = str(
            request.payload.get("sub_sovereign") or "system-sub-sovereign"
        )
        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome(
                "NOT_CODEX_CHILD", self.verified_basis("A284", "A334")
            )
        child = self._sub_sovereigns.get(child_id)
        if child is None:
            return refusal_outcome(
                "SUB_SOVEREIGN_NOT_REGISTERED", self.verified_basis("A284")
            )

        return accepted_outcome(
            {
                "delegated_to": child_id,
                "action": request.payload.get("action", "coordinate"),
                "primary_domain": primary_domain_of(child_id),
                "started": bool(getattr(child, "started", False)),
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A284", "A287", "A130", "A334"),
        )

    async def _adjudicate_health_coordinate(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """與維護主宰協調健康（A33/A65）。"""
        return accepted_outcome(
            {
                "coordinated_with": "health-maintenance-test-sub-sovereign",
                "scope": "runtime-integrity",
                "information_layer": "official",
            },
            self.verified_basis("A28", "A33", "A65"),
        )

    # ------------------------------------------------------------------
    # A334 child lifecycle adjudication
    # ------------------------------------------------------------------

    def _adjudicate_child_activation(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: authorize activation of a specific registered child."""
        child_id = str(request.payload.get("sub_sovereign", ""))
        return self.authorize_child_activation(child_id)

    def _adjudicate_child_deactivation(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: authorize deactivation of a registered child."""
        child_id = str(request.payload.get("sub_sovereign", ""))
        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome("NOT_CODEX_CHILD", ("A334",))
        if child_id not in self._sub_sovereigns:
            return refusal_outcome("CHILD_NOT_REGISTERED", ("A334", "A130"))
        return accepted_outcome(
            {
                "child": child_id,
                "parent": self.sovereign_id,
                "deactivation": "authorized",
                "execution": "delegated-to-governed-executor",
            },
            self.verified_basis("A334", "A130"),
        )

    def _adjudicate_child_failure(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322 retry/cancel: adjudicate a reported child failure.

        Bounded restart budget — a child may be restarted up to
        ``_MAX_CHILD_RESTARTS`` consecutive failures; beyond that the
        adjudication cancels restarts and marks the child quarantined.
        """
        child_id = str(request.payload.get("sub_sovereign", ""))
        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome("NOT_CODEX_CHILD", ("A322", "A334"))
        outcome = str(request.payload.get("outcome") or "failure").casefold()
        if outcome in ("success", "recovered", "converged"):
            self.record_child_success(child_id)
            return accepted_outcome(
                {"child": child_id, "failure_count": 0, "action": "cleared"},
                self.verified_basis("A322"),
            )
        count = self.record_child_failure(child_id)
        if count > _MAX_CHILD_RESTARTS:
            self._isolated_children.add(child_id)
            return accepted_outcome(
                {
                    "child": child_id,
                    "failure_count": count,
                    "action": "quarantine",
                    "restart": "denied-budget-exhausted",
                    "max_restarts": _MAX_CHILD_RESTARTS,
                },
                self.verified_basis("A322"),
            )
        return accepted_outcome(
            {
                "child": child_id,
                "failure_count": count,
                "action": "restart",
                "remaining_attempts": _MAX_CHILD_RESTARTS - count,
                "execution": "delegated-to-governed-executor",
            },
            self.verified_basis("A322", "A334"),
        )

    def _adjudicate_module_route(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: route an executable module to its managing runtime child."""
        from ..registries import module_assignment

        module_code = str(request.payload.get("module", ""))
        assignment = module_assignment(module_code)
        if assignment is None:
            return refusal_outcome("MODULE_NOT_REGISTERED", ("A334",))
        owner = str(assignment.get("managing_sub_sovereign") or "")
        if not validate_child_parent(owner, self.sovereign_id):
            return refusal_outcome(
                "MODULE_OWNER_NOT_RUNTIME_CHILD",
                self.verified_basis("A334", "A28"),
            )
        child = self._sub_sovereigns.get(owner)
        return accepted_outcome(
            {
                "module": module_code,
                "route_to": owner,
                "primary_domain": primary_domain_of(owner),
                "materialized": child is not None,
                "decision_authority": assignment.get("decision_authority"),
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A334", "A28"),
        )

    def _adjudicate_retry_cancel(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: adjudicate retry vs cancel for a runtime child."""
        child_id = str(request.payload.get("sub_sovereign", ""))
        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome(
                "NOT_CODEX_CHILD", self.verified_basis("A322", "A334")
            )
        try:
            attempt = int(request.payload.get("attempt") or 0)
            max_attempts = int(
                request.payload.get("max_attempts") or _MAX_CHILD_RESTARTS
            )
        except (TypeError, ValueError):
            return refusal_outcome("INVALID_ATTEMPT_COUNT", ("A322",))
        if attempt < max_attempts:
            return accepted_outcome(
                {
                    "child": child_id,
                    "action": "retry",
                    "attempt": attempt + 1,
                    "remaining_attempts": max_attempts - attempt - 1,
                },
                self.verified_basis("A322"),
            )
        return accepted_outcome(
            {
                "child": child_id,
                "action": "cancel",
                "reason": "retry-budget-exhausted",
                "attempt": attempt,
            },
            self.verified_basis("A322"),
        )

    def _adjudicate_convergence(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: convergence acceptance — all reported children must
        have reached a converged/success state; fail-closed otherwise."""
        raw = request.payload.get("results")
        if not isinstance(raw, dict) or not raw:
            return refusal_outcome("MISSING_RESULTS", ("A322",))
        incomplete: list[str] = []
        for child_id, status in raw.items():
            child = str(child_id)
            if not validate_child_parent(child, self.sovereign_id):
                return refusal_outcome(
                    "NOT_CODEX_CHILD", self.verified_basis("A322", "A334")
                )
            if str(status).casefold() not in (
                "converged", "success", "succeeded", "ready", "serving",
            ):
                incomplete.append(child)
        if incomplete:
            from core_system.codex_decision import Refusal, SovereignOutcome, verified_basis
            basis = verified_basis(("A322",))
            return SovereignOutcome(
                accepted=False,
                refusal=Refusal("CONVERGENCE_INCOMPLETE", basis),
                result={"incomplete": incomplete},
                basis=basis,
            )
        return accepted_outcome(
            {
                "converged_children": sorted(str(c) for c in raw),
                "acceptance": "granted",
            },
            self.verified_basis("A322"),
        )

    def _adjudicate_conflict_isolation(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: adjudicate conflict isolation between runtime children."""
        raw = request.payload.get("conflicting")
        if not isinstance(raw, (list, tuple)) or not raw:
            return refusal_outcome("MISSING_CONFLICTING_SET", ("A322",))
        conflicting = [str(c) for c in raw]
        for child_id in conflicting:
            if not validate_child_parent(child_id, self.sovereign_id):
                return refusal_outcome(
                    "NOT_CODEX_CHILD", self.verified_basis("A322", "A334")
                )
        return accepted_outcome(
            {
                "isolated_children": conflicting,
                "isolation": "dispatch-suspended",
                "re_entry": "requires-convergence-acceptance",
            },
            self.verified_basis("A322"),
        )

    # ------------------------------------------------------------------
    # Auto-automation loop (A28/A33/A65 full-automation upgrade)
    # ------------------------------------------------------------------

    async def start_auto_loop(self) -> None:
        """Start the background auto-automation loop.

        The loop periodically:
        1. Checks coverage gaps and routes them to the decision-sovereign.
        2. Monitors runtime readiness state (runtime-readiness.json).
        3. Detects failed children and triggers retry/cancel adjudication.
        4. Coordinates health with the maintenance sub-sovereign.
        5. Detects convergence and auto-accepts.
        6. Detects conflicts and auto-isolates.

        The sovereign remains decision-only (A28): the loop adjudicates
        and routes; it does not execute.  All adjudications go through
        ``handle()`` (A10/A11 fail-closed).
        """
        if self._auto_loop_task is not None and not self._auto_loop_task.done():
            return
        self._auto_enabled = True
        self._auto_loop_task = asyncio.create_task(self._auto_loop())

    async def stop_auto_loop(self) -> None:
        """Stop the background auto-automation loop."""
        self._auto_enabled = False
        task = self._auto_loop_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._auto_loop_task = None

    async def _auto_loop(self) -> None:
        """Background loop: periodic automation checks."""
        while self._auto_enabled:
            try:
                await self._auto_cycle()
            except asyncio.CancelledError:
                break
            except Exception as error:
                _logger.warning("runtime auto-cycle error: %s", error)
            await asyncio.sleep(self._auto_loop_interval)

    async def _auto_cycle(self) -> None:
        """One automation cycle: coverage, readiness, retry, health, convergence, conflict."""
        self._auto_metrics["last_auto_cycle"] = self._iso_now()
        self._auto_metrics["coverage_checks"] += 1

        # 1. Coverage gap detection and repair routing.
        await self._auto_coverage_repair()

        # 2. Runtime readiness state monitoring.
        await self._auto_readiness_monitor()

        # 3. Child failure detection and retry/cancel.
        await self._auto_child_retry()

        # 4. Health coordination.
        await self._auto_health_coordinate()

        # 5. Convergence detection and acceptance.
        await self._auto_convergence_check()

        # 6. Conflict detection and isolation.
        await self._auto_conflict_detect()

    async def _auto_coverage_repair(self) -> None:
        """A334: detect coverage gaps and auto-route to decision-sovereign."""
        report = self.coverage_gap_report()
        if report is None:
            return
        self._auto_metrics["gap_repairs_routed"] += 1
        decision = getattr(self.app, "decision_sovereign", None)
        if decision is None:
            _logger.warning(
                "runtime coverage gap detected but decision-sovereign unavailable: %s",
                report.get("affected_children"),
            )
            return
        try:
            request = SovereignRequest(
                intent="repair.decide-and-route",
                subject="runtime-coverage-gap",
                requester=self.sovereign_id,
                payload={
                    "classified_signal": {
                        "repair_type": "coverage-gap",
                        "target": "system-runtime-sovereign",
                        "affected_children": report.get("affected_children", []),
                        "gap_type": report.get("gap_type", "missing"),
                        "coverage_ratio": report.get("coverage_ratio", 0.0),
                    },
                },
            )
            await decision.handle(request)
        except Exception as error:
            _logger.warning("runtime coverage gap route failed: %s", error)

    async def _auto_readiness_monitor(self) -> None:
        """A28/A65: monitor runtime-readiness.json for state transitions.

        Detects degraded/dead states and auto-routes to the
        decision-sovereign's repair-decision chain (A152/A154).
        """
        self._auto_metrics["runtime_state_polls"] += 1
        readiness = self._read_runtime_readiness()
        if readiness is None:
            return

        # Extract the overall state.
        snapshot = readiness.get("snapshot", readiness)
        overall_state = str(
            snapshot.get("overall_state") or snapshot.get("runtime_state") or ""
        ).casefold()
        overall_ready = snapshot.get("overall_ready")

        # Update internal runtime state.
        if overall_ready is True:
            self._runtime_state = "serving"
        elif overall_state in _DEGRADED_STATES:
            self._runtime_state = overall_state
        elif overall_state in _SERVING_STATES:
            self._runtime_state = "serving"

        # Detect state transitions to degraded/dead.
        if (
            overall_state in _DEGRADED_STATES
            and overall_state != self._last_readiness_state
        ):
            self._auto_metrics["degradation_detected"] += 1
            self._last_readiness_state = overall_state
            # Route to decision-sovereign for repair.
            decision = getattr(self.app, "decision_sovereign", None)
            if decision is not None:
                try:
                    request = SovereignRequest(
                        intent="repair.decide-and-route",
                        subject="runtime-degradation",
                        requester=self.sovereign_id,
                        payload={
                            "classified_signal": {
                                "repair_type": "runtime-degradation",
                                "target": "system-runtime-sovereign",
                                "runtime_state": overall_state,
                                "overall_ready": overall_ready,
                                "startup_failures": snapshot.get(
                                    "startup_failures", []
                                ),
                            },
                        },
                    )
                    await decision.handle(request)
                except Exception as error:
                    _logger.warning(
                        "runtime degradation route failed: %s", error
                    )
        elif overall_state not in _DEGRADED_STATES:
            self._last_readiness_state = overall_state

    def _read_runtime_readiness(self) -> dict[str, Any] | None:
        """Read the runtime-readiness.json information-layer state file."""
        project_root = getattr(self.app, "project_root", None)
        if project_root is None:
            project_root = Path.cwd()
        readiness_path = Path(project_root).joinpath(*_READINESS_STATE_RELATIVE)
        try:
            payload = json.loads(readiness_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    async def _auto_child_retry(self) -> None:
        """A322: detect failed children and auto-trigger retry/cancel."""
        for child_id, count in list(self._child_failure_counts.items()):
            if count <= 0:
                continue
            if not validate_child_parent(child_id, self.sovereign_id):
                continue
            if child_id in self._isolated_children:
                continue
            if count > _MAX_CHILD_RESTARTS:
                self._isolated_children.add(child_id)
                self._auto_metrics["child_quarantines"] += 1
                _logger.warning(
                    "runtime child quarantined (budget exhausted): %s", child_id
                )
                continue
            self._auto_metrics["child_retries_triggered"] += 1
            try:
                request = SovereignRequest(
                    intent="runtime.retry-cancel",
                    subject=f"auto-retry-{child_id}",
                    requester=self.sovereign_id,
                    payload={
                        "sub_sovereign": child_id,
                        "attempt": count,
                        "max_attempts": _MAX_CHILD_RESTARTS,
                    },
                )
                await self.handle(request)
            except Exception as error:
                _logger.warning(
                    "runtime auto-retry for %s failed: %s", child_id, error
                )

    async def _auto_health_coordinate(self) -> None:
        """A33/A65: auto-coordinate health with maintenance sub-sovereign.

        Detects runtime integrity issues (degraded state, failed children)
        and coordinates with the health-maintenance-test-sub-sovereign.
        """
        has_issues = (
            self._runtime_state in _DEGRADED_STATES
            or any(
                count > 0
                for name, count in self._child_failure_counts.items()
                if name not in self._isolated_children
            )
        )
        if not has_issues:
            return
        self._auto_metrics["health_coordinations"] += 1
        try:
            request = SovereignRequest(
                intent="health.coordinate",
                subject="auto-health-coordination",
                requester=self.sovereign_id,
                payload={
                    "runtime_state": self._runtime_state,
                    "failed_children": [
                        name
                        for name, count in self._child_failure_counts.items()
                        if count > 0 and name not in self._isolated_children
                    ],
                },
            )
            await self.handle(request)
        except Exception as error:
            _logger.warning("runtime auto-health coordinate failed: %s", error)

    async def _auto_convergence_check(self) -> None:
        """A322: detect convergence across all started children and auto-accept."""
        started_children = {
            name: sov
            for name, sov in self._sub_sovereigns.items()
            if getattr(sov, "_started", False) or getattr(sov, "started", False)
        }
        if not started_children:
            return

        results: dict[str, str] = {}
        for name, sov in started_children.items():
            if name in self._isolated_children:
                results[name] = "isolated"
                continue
            status_method = getattr(sov, "live_status", None)
            if status_method is None:
                results[name] = "unknown"
                continue
            try:
                status = status_method()
                state = str(status.get("state", "unknown")).casefold()
                if state in ("converged", "ready", "active", "serving", "synced"):
                    results[name] = "converged"
                else:
                    results[name] = state
            except Exception:
                results[name] = "unknown"

        all_converged = all(
            v in ("converged", "isolated") for v in results.values()
        )
        if all_converged and results:
            prev = {
                k: v
                for k, v in self._child_convergence.items()
                if k in results
            }
            if prev != results:
                self._auto_metrics["convergence_accepts"] += 1
                self._child_convergence = dict(results)
                try:
                    request = SovereignRequest(
                        intent="runtime.convergence-acceptance",
                        subject="auto-convergence",
                        requester=self.sovereign_id,
                        payload={"results": results},
                    )
                    await self.handle(request)
                except Exception as error:
                    _logger.warning(
                        "runtime auto-convergence accept failed: %s", error
                    )

    async def _auto_conflict_detect(self) -> None:
        """A322: detect conflicts between children and auto-isolate.

        If 2+ children are failing simultaneously, isolate them to
        prevent cascading failures.
        """
        conflicting: list[str] = []
        for name, sov in self._sub_sovereigns.items():
            if name in self._isolated_children:
                continue
            if not getattr(sov, "_started", False):
                continue
            count = self._child_failure_counts.get(name, 0)
            if count > 0:
                conflicting.append(name)

        if len(conflicting) >= 2:
            self._auto_metrics["conflict_isolations"] += 1
            try:
                request = SovereignRequest(
                    intent="runtime.conflict-isolation",
                    subject="auto-conflict",
                    requester=self.sovereign_id,
                    payload={"conflicting": conflicting},
                )
                await self.handle(request)
                for c in conflicting:
                    self._isolated_children.add(c)
            except Exception as error:
                _logger.warning(
                    "runtime auto-conflict isolate failed: %s", error
                )

    def re_accept_child(self, child_id: str) -> bool:
        """A322: re-accept a previously isolated child.

        Clears the isolation flag and resets the failure counter so the
        child can resume normal dispatch.  Returns True if the child was
        isolated and is now re-accepted; False otherwise.
        """
        if child_id not in self._isolated_children:
            return False
        self._isolated_children.discard(child_id)
        self._child_failure_counts.pop(child_id, None)
        return True

    async def _on_start(self) -> None:
        """Start the auto-automation loop when the sovereign starts."""
        await self.start_auto_loop()

    async def _on_stop(self) -> None:
        """Stop the auto-automation loop when the sovereign stops."""
        await self.stop_auto_loop()

    def auto_status(self) -> dict[str, Any]:
        """Read-only status of the auto-automation subsystem."""
        return {
            "enabled": self._auto_enabled,
            "loop_running": (
                self._auto_loop_task is not None
                and not self._auto_loop_task.done()
            ),
            "loop_interval_seconds": self._auto_loop_interval,
            "metrics": dict(self._auto_metrics),
            "isolated_children": sorted(self._isolated_children),
            "child_convergence": dict(self._child_convergence),
            "last_readiness_state": self._last_readiness_state,
        }

    # ------------------------------------------------------------------
    # Coverage (A334)
    # ------------------------------------------------------------------

    def sync_coverage(self) -> dict[str, Any]:
        """A334 coverage: codex-expected children vs materialized/started."""
        expected = children_of(self.sovereign_id)
        materialized = set(self._sub_sovereigns)
        started = {
            name
            for name, sov in self._sub_sovereigns.items()
            if getattr(sov, "_started", False) or getattr(sov, "started", False)
        }
        missing = sorted(set(expected) - materialized)
        not_started = sorted(materialized - started)
        return {
            "authority": "codex-A334",
            "expected_children": sorted(expected),
            "materialized": sorted(materialized & set(expected)),
            "started": sorted(started),
            "missing": missing,
            "not_started": not_started,
            "unexpected": sorted(materialized - set(expected)),
            "coverage": (
                len(materialized & set(expected)) / len(expected)
                if expected
                else 1.0
            ),
        }

    def coverage_gap_report(self) -> dict[str, Any] | None:
        """A334/A322: report a coverage gap for repair routing.

        Returns a structured gap report when missing or not-started
        children are detected, or None when coverage is complete.
        """
        coverage = self.sync_coverage()
        missing = coverage.get("missing", [])
        not_started = coverage.get("not_started", [])
        if not missing and not not_started:
            return None
        return {
            "sovereign": self.sovereign_id,
            "gap_type": "missing" if missing else "not-started",
            "affected_children": missing or not_started,
            "coverage_ratio": coverage.get("coverage", 0.0),
            "repair_route": "decision-sovereign.repair-decision",
            "authority": "codex-A334",
        }

    # ------------------------------------------------------------------
    # Legacy compatibility
    # ------------------------------------------------------------------

    def set_sub_sovereign(self, sovereign: Any) -> None:
        """Legacy setter — registers into the unified A334 child registry."""
        identity = (
            getattr(sovereign, "sovereign_id", None) or "system-sub-sovereign"
        )
        self.register_sub_sovereign(identity, sovereign)

    def set_runtime_state(self, state: str) -> None:
        self._runtime_state = state

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["runtime_state"] = self._runtime_state
        base["sub_sovereigns"] = {
            name: (
                sov.live_status() if hasattr(sov, "live_status")
                else {"role": name}
            )
            for name, sov in self._sub_sovereigns.items()
        }
        base["coverage"] = self.sync_coverage()
        base["auto"] = self.auto_status()
        return base

    def orchestration_status(self) -> dict[str, Any]:
        children = {
            name: {
                "primary_domain": primary_domain_of(name),
                "started": bool(
                    getattr(sov, "_started", False)
                    or getattr(sov, "started", False)
                ),
                "failure_count": self._child_failure_counts.get(name, 0),
            }
            for name, sov in self._sub_sovereigns.items()
        }
        return {
            "state": "active" if self._started else "stopped",
            "owner": self.role,
            "authority": "codex-A334",
            "runtime_state": self._runtime_state,
            "children": children,
            "coverage": self.sync_coverage(),
            "failure_counts": dict(self._child_failure_counts),
            "auto": self.auto_status(),
            "delegation": "governed-executor-only",
        }


__all__ = ["SystemRuntimeSovereign"]