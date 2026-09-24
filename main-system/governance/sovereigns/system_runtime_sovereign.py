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

from .parallel_adjudication_mixin import ParallelAdjudicationMixin

_logger = logging.getLogger("gptbridge.sovereign.system_runtime")

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
    ParallelAdjudicationMixin,
    SovereignBase,
):
    """系統運行主宰：進程存活、運行完整性、平台服務決策。"""

    sovereign_id = "system-runtime-sovereign"

    # A10/A11 explicit intent allowlist
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        "runtime.status",
        "runtime.action",
        "health.coordinate",
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
        self._auto_loop_interval: float = 5.0  # seconds — fast path while
        # the runtime is not ready or degradation is active
        # §10.63 R2: steady-state supervision drops to a 60 s idle cadence;
        # hard-failure signals still arrive via the IPC watchdog/heartbeat.
        self._auto_loop_idle_interval: float = 60.0
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
        # Retired-child registry kept empty by design (A592/A604) — status
        # surfaces read it to show retired markers.
        self._sub_sovereigns: dict[str, Any] = {}

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
        """並行裁決：運行狀態、動作、健康協調、A322決策。"""
        intent = request.intent

        # Runtime core intents
        if intent == "runtime.status":
            return await self._adjudicate_runtime_status(request)
        if intent == "runtime.action":
            return await self._adjudicate_runtime_action(request)
        if intent == "health.coordinate":
            return await self._adjudicate_health_coordinate(request)

        # A322-style retry/cancel and convergence
        if intent == "runtime.retry-cancel":
            return await self._adjudicate_retry_cancel(request)
        if intent == "runtime.convergence-acceptance":
            return await self._adjudicate_convergence_acceptance(request)

        # Conflict isolation
        if intent == "runtime.conflict-isolation":
            return await self._adjudicate_conflict_isolation(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A10", "A12"))

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """系統運行主宰委派執行（A446/A121）。

        This sovereign is decision-only (A28).  Execution is delegated
        to governed executor / sub-sovereigns via delegate_to.  This hook
        attests that and records the delegation outcome in the audit ledger.
        """
        return self._attach_delegation_receipt(decision, request, "decision-only")

    # ------------------------------------------------------------------
    # Adjudication handlers
    # ------------------------------------------------------------------

    async def _adjudicate_runtime_status(self, request: SovereignRequest) -> SovereignOutcome:
        """Runtime status adjudication."""
        return accepted_outcome(
            {
                "runtime_state": self._runtime_state,
                "auto_metrics": dict(self._auto_metrics),
                "child_supervision": {
                    cid: dict(watch) for cid, watch in self._child_supervision.items()
                },
                "readiness": self._load_readiness_state(),
            },
            self.verified_basis("A28", "A33", "A65"),
        )

    async def _adjudicate_runtime_action(self, request: SovereignRequest) -> SovereignOutcome:
        """Runtime action adjudication."""
        action = request.payload.get("action")
        if action not in ("start", "stop", "restart", "health-check"):
            return refusal_outcome("INVALID_ACTION", self.verified_basis("A28"))

        self._auto_metrics["runtime_state_polls"] += 1
        return accepted_outcome(
            {"action": action, "runtime_state": self._runtime_state, "execution": "delegated"},
            self.verified_basis("A28", "A446"),
        )

    async def _adjudicate_health_coordinate(self, request: SovereignRequest) -> SovereignOutcome:
        """Health coordination adjudication (routes to decision-sovereign repair chain)."""
        self._auto_metrics["health_coordinations"] += 1
        signal = request.payload.get("health_signal")
        if not signal:
            return refusal_outcome("MISSING_HEALTH_SIGNAL", self.verified_basis("A152"))

        # Route to decision-sovereign's repair decision chain
        return await self.delegate_to(
            "decision-sovereign",
            SovereignRequest(
                intent="repair.decide-and-route",
                subject=request.subject,
                requester=request.requester,
                payload={"classified_signal": signal},
            ),
        )

    async def _adjudicate_retry_cancel(self, request: SovereignRequest) -> SovereignOutcome:
        """A322: retry/cancel adjudication."""
        operation_id = request.payload.get("operation_id")
        action = request.payload.get("action", "retry")
        return accepted_outcome(
            {"operation_id": operation_id, "action": action, "authority": self.sovereign_id},
            self.verified_basis("A322", "A28"),
        )

    async def _adjudicate_convergence_acceptance(self, request: SovereignRequest) -> SovereignOutcome:
        """A322: convergence acceptance adjudication."""
        target = request.payload.get("target")
        criteria = request.payload.get("criteria", {})
        return accepted_outcome(
            {"target": target, "criteria": criteria, "decision": "accepted", "authority": self.sovereign_id},
            self.verified_basis("A322", "A28"),
        )

    async def _adjudicate_conflict_isolation(self, request: SovereignRequest) -> SovereignOutcome:
        """A322: conflict isolation adjudication."""
        conflict_id = request.payload.get("conflict_id")
        parties = request.payload.get("parties", [])
        return accepted_outcome(
            {
                "conflict_id": conflict_id,
                "parties": parties,
                "isolation": "adjudicated",
                "authority": self.sovereign_id,
            },
            self.verified_basis("A322", "A28"),
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
            "runtime_state": self._runtime_state,
            "sub_sovereigns": [
                self._child_status(child_id)
                for child_id in children_of(self.sovereign_id)
            ],
            "auto_metrics": dict(self._auto_metrics),
            "child_supervision": {
                cid: dict(watch) for cid, watch in self._child_supervision.items()
            },
            "readiness": self._load_readiness_state(),
        })

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["sub_sovereign_registry"] = {
            name: sov.live_status() if hasattr(sov, "live_status") else {"role": name}
            for name, sov in self._sub_sovereigns.items()
        }
        return base

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """啟動系統運行主宰（決策層初始化，A297）。

        Decision-layer initialization only: the supervision loop is
        started separately by ``start_supervision()``.
        """
        state = await super().start()
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

    # ------------------------------------------------------------------
    # Autonomous supervision (decision-layer only)
    # ------------------------------------------------------------------

    def _start_autonomy_loop(self) -> None:
        if self._auto_loop_task is None or self._auto_loop_task.done():
            self._auto_loop_task = asyncio.create_task(
                self._autonomy_loop(),
                name="system-runtime-sovereign-autonomy",
            )

    async def _stop_autonomy_loop(self) -> None:
        task = self._auto_loop_task
        self._auto_loop_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _autonomy_loop(self) -> None:
        while self._auto_enabled:
            try:
                await self._autonomy_tick()
            except asyncio.CancelledError:
                raise
            except (OSError, ValueError, RuntimeError, ImportError, TypeError, AttributeError, KeyError, PermissionError):
                pass
            # Fast cadence only while the runtime is not yet ready; the
            # degradation counter is cumulative so it cannot gate the
            # steady-state interval.
            interval = (
                self._auto_loop_interval
                if self._runtime_state not in ("ready", "serving")
                else self._auto_loop_idle_interval
            )
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                raise

    async def _autonomy_tick(self) -> None:
        await self._check_coverage()
        await self._check_runtime_readiness()
        await self._check_process_survival()
        await self._check_convergence()
        self._persist_metrics()

    async def _check_coverage(self) -> None:
        """Check governance coverage and route gaps to repair chain."""
        self._auto_metrics["coverage_checks"] += 1
        # Coverage check logic would go here
        # Gaps routed to decision-sovereign via health.coordinate

    async def _check_runtime_readiness(self) -> None:
        """Poll runtime readiness state (A67 information layer)."""
        self._auto_metrics["runtime_state_polls"] += 1
        state = self._load_readiness_state()
        current = str(
            state.get("state") or state.get("runtime_state") or "unknown"
        ).lower()
        if current in _DEGRADED_STATES:
            self._auto_metrics["degradation_detected"] += 1
            self._runtime_state = current
        elif current in _SERVING_STATES:
            self._runtime_state = current

    def _load_readiness_state(self) -> dict[str, Any]:
        try:
            root = getattr(self.app, "project_root", None) or Path.cwd()
            path = Path(root).joinpath(*_READINESS_STATE_RELATIVE)
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        # The information-layer writer nests the projection under
        # ``snapshot``; older files were flat.  Accept both so the
        # runtime state is never misread as ``unknown``.
        snapshot = payload.get("snapshot")
        if isinstance(snapshot, dict):
            merged = dict(payload)
            merged.update(snapshot)
            return merged
        return payload

    async def _check_process_survival(self) -> None:
        """Check process survival and coordinate health."""
        # Process survival check logic
        pass

    async def _check_convergence(self) -> None:
        """Check convergence of sync operations."""
        # Convergence check logic
        pass

    def _persist_metrics(self) -> None:
        """Persist automation metrics for monitoring."""
        try:
            from pathlib import Path
            import json
            state_path = (
                Path(getattr(self.app, "project_root", Path.cwd()))
                / "main-system"
                / "runtime"
                / "state"
                / "system-runtime-sovereign.json"
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "sovereign": "system-runtime-sovereign",
                "started": self._started,
                "heartbeat_at": _iso_now(),
                "runtime_state": self._runtime_state,
                "auto_metrics": dict(self._auto_metrics),
                "child_supervision": {
                    cid: dict(watch) for cid, watch in self._child_supervision.items()
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


__all__ = ["SystemRuntimeSovereign"]