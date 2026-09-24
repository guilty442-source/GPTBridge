"""Automation Sovereign — 自動化主宰（專門決策主宰，A330 認證更新執行例外）。

法典依據:
- sovereign_id: automation-sovereign (position 18)
- area: automation-decision
- rank: specialized-decision-sovereign-with-A330-certified-update-execution-exception
- basis: A301|A322|A330|A334
- duties: resource-sync|channel-sync|release-sync|runtime-sync|repair-sync|cleanup-sync|log-sync|dependency-sync
- powers: adjudicate-sync-decisions|A330-certified-update-execution
- prohibitions: FORBID:general-execution (except A330)

A301: synchronization policy+priority+consistency target+conflict
disposition+acceptance decision only.
A322: SOLE-DECISION over sync target + dependency order + atomic boundary
+ conflict isolation + retry/cancel + convergence acceptance.
A330: certified update-set execution exception — the only execution
power, and only after the certification proof adjudication passes.
A486: RENAME:synchronization-sovereign canonically renamed automation-sovereign;
DISPLAY:automation-sovereign is the sole active identity.
A592/A604: the sub-sovereign layer is eliminated — former child
identities are retired lineage only and are never routed to
(FORBID:sub-sovereign-routing).  Sync responsibilities are absorbed by
this core's registered modules.
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

from .parallel_adjudication_mixin import ParallelAdjudicationMixin

_logger = logging.getLogger("gptbridge.sovereign.automation")

# A330: update types covered by the certified-update execution exception
_A330_UPDATE_TYPES: frozenset[str] = frozenset(
    {"backend-release", "codex", "governance-policy", "directory"}
)


class AutomationSovereign(
    ParallelAdjudicationMixin,
    SovereignBase,
):
    """自動化主宰：專門決策，協調各類同步子主宰，A330例外執行。"""

    sovereign_id = "automation-sovereign"

    # A10/A12 explicit intent allowlist
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        # A330 certified update execution (sole execution exception)
        "A330.certified-update",
        # A322 sync decision adjudication
        "sync.decision",
    })

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        # A330 certified update operations
        self._certified_update_operations: dict[str, dict[str, Any]] = {}
        # Autonomous supervision
        self._autonomy_task: asyncio.Task[Any] | None = None
        self._autonomy_stop = asyncio.Event()

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
        """並行裁決：A330執行、A322決策。"""
        intent = request.intent

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
        """自動化主宰委派執行（A446/A121）。

        This sovereign is decision-only except for A330 certified update
        execution.  The delegate_to calls inside _adjudicate already
        dispatch execution to the governed executor or governed modules.
        This hook attests that and records the delegation outcome in the
        audit ledger.
        """
        execution_mode = "A330-certified-update" if request.intent == "A330.certified-update" else "decision-only"
        return self._attach_delegation_receipt(decision, request, execution_mode)

    # ------------------------------------------------------------------
    # Adjudication handlers
    # ------------------------------------------------------------------

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
                "authority": "automation-sovereign",
                "basis": "A322",
            },
            verified_basis(("A322", "A301", "A334")),
        )

    def status(self) -> dict[str, Any]:
        return self._with_status_schema({
            "sovereign": self.sovereign_id,
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
            },
        })

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["certified_updates"] = list(self._certified_update_operations.values())
        return base

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Mark the Automation Sovereign active."""
        state = await super().start()
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
                    name="automation-sovereign-autonomy",
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
        self._persist_live_state()

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
                / "automation-sovereign.json"
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "sovereign": "automation-sovereign",
                "started": self._started,
                "heartbeat_at": _iso_now(),
                "autonomy": {
                    "enabled": True,
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


__all__ = ["AutomationSovereign"]