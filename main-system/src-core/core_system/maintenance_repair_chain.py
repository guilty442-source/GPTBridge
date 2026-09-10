"""Maintenance Sovereign — A152/A154 health-classification chain mixin.

Implements the maintenance sovereign's **health-only** role in the repair
flow per the amended Governance Codex (A152 supersedes A67, A154 supersedes
A72, E127 supersedes E48, E128 supersedes E52):

  health-signal > maintenance-classification > system-decision
  > permission > runtime-or-programming > executor > verification
  > information-layer > ui

The maintenance sovereign (A125/E102) owns system HEALTH only
(monitor-system-health / preserve-system-health / maintain-system).  It
**classifies** incoming health signals from the information layer and
delegates the repair **decision** to the system-decision-sovereign
(A152: ``REPAIR-DECISION:system-decision-sovereign``;
``FORBID:maintenance-owning-non-health-decisions``).  After the
system-decision-sovereign routes the repair through permission validation
and governed execution, the maintenance sovereign records the outcome in
the learning store (E127: ``LEARNING:learning-system``) and syncs the UI.

This mixin polls ``repair-requests.json`` for pending signals, classifies
them, delegates the decision, acknowledges the result, audits the outcome,
and syncs the UI.  The repair decision, permission validation, dispatch
and independent verification live in ``repair_decision_chain.py`` under the
system-decision-sovereign.

Extracted from ``maintenance_update`` to keep each module focused and
under 500 lines.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .sovereign_utils import _iso_now


class MaintenanceRepairChainMixin:
    """A152/A154 health-classification chain for the Maintenance Sovereign.

    The maintenance sovereign classifies health signals and delegates the
    repair decision to the system-decision-sovereign.  It never owns the
    repair decision, permission validation, or code mutation (A154:
    ``MAINTENANCE-SCOPE:health-only``; ``FORBID:maintenance-code-change``).

    Expects the following attributes to be set by the composing class's
    ``__init__``:

      * ``self.app`` — the GPTBridgeApp instance
      * ``self._repair_decision_task`` — asyncio.Task or None
      * ``self._started`` — bool
      * ``self.ROLE``
    """

    _REPAIR_POLL_INTERVAL_SECONDS: float = 5.0

    # ------------------------------------------------------------------
    # Status (used by MaintenanceStatusMixin)
    # ------------------------------------------------------------------

    def _repair_decision_chain_status(self) -> dict[str, Any]:
        """Report the health-classification chain status for observability."""
        task = getattr(self, "_repair_decision_task", None)
        return {
            "enabled": task is not None and not task.done(),
            "authority": self.ROLE,
            "scope": "health-classification",
            "decision_authority": "system-decision-sovereign",
            "chain": "A152/A154",
            "poll_interval_seconds": self._REPAIR_POLL_INTERVAL_SECONDS,
        }

    # ------------------------------------------------------------------
    # Lifecycle hooks (called by MaintenanceLifecycleMixin)
    # ------------------------------------------------------------------

    def _start_repair_decision_loop(self) -> None:
        """Start the health-signal classification loop.  Called from ``start()``."""
        if getattr(self, "_repair_decision_task", None) is None:
            self._repair_decision_task = asyncio.create_task(
                self._repair_decision_loop(),
                name="maintenance-sovereign-health-classification",
            )

    async def _stop_repair_decision_loop(self) -> None:
        """Stop the health-signal classification loop.  Called from ``stop()``."""
        task = getattr(self, "_repair_decision_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._repair_decision_task = None

    # ------------------------------------------------------------------
    # Health-signal classification loop
    # ------------------------------------------------------------------

    async def _repair_decision_loop(self) -> None:
        """Poll ``repair-requests.json`` and classify pending health signals.

        This is the maintenance sovereign's health-classification entry
        point.  Per A152: the maintenance sovereign classifies health
        signals (health-only scope, A154) and delegates the repair
        DECISION to the system-decision-sovereign.
        """
        while not self._stop_requested():
            try:
                await asyncio.to_thread(self._process_pending_repair_requests)
            except asyncio.CancelledError:
                raise
            except Exception:
                pass  # Best-effort; never crash the sovereign.
            try:
                await asyncio.sleep(self._REPAIR_POLL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise

    def _process_pending_repair_requests(self) -> None:
        """Process all pending repair requests from the information layer."""
        from tasks.repair_coordinator import get_repair_coordinator

        coordinator = get_repair_coordinator()
        if coordinator is None:
            return

        pending = coordinator.pending_requests()
        for request in pending:
            try:
                self._handle_repair_request(coordinator, request)
            except Exception:
                pass  # Best-effort; acknowledge to prevent reprocessing.

    def _handle_repair_request(
        self,
        coordinator: Any,
        request: dict[str, Any],
    ) -> None:
        """Classify a health signal and delegate the repair decision.

        Per E128: ``health-signal>maintenance-classification>system-decision
        >permission>runtime-or-programming>executor>verification>
        information-layer>ui``.  The maintenance sovereign classifies the
        health signal (health-only, A154) and delegates the repair DECISION
        to the system-decision-sovereign (A152).  It then records the
        outcome in the learning store (E127) and syncs the UI.
        """
        request_id = str(request.get("request_id") or "")
        decision_proof = request.get("decision_proof") or {}

        # ── Step 1: health classification (maintenance scope: health-only) ──
        classified = self._classify_health_signal(decision_proof)

        # ── Step 2: delegate repair decision to system-decision-sovereign ──
        system_sovereign = getattr(self.app, "system_sovereign_service", None)
        if system_sovereign is None:
            coordinator.acknowledge_request(
                request_id,
                system_decision="denied-no-system-decision-sovereign",
                ok=False,
            )
            self._audit_repair_outcome(request, classified, None, ok=False)
            return

        result = system_sovereign.decide_and_route_repair(classified)
        ok = bool(result.get("ok"))
        decision = str(result.get("decision") or "")

        # ── Step 3: acknowledge in information layer ──
        coordinator.acknowledge_request(
            request_id,
            system_decision=decision,
            ok=ok,
        )

        # ── Step 4: record outcome in learning store (E127: learning-system) ──
        self._audit_repair_outcome(request, classified, result, ok=ok)

        # ── Step 5: UI sync ──
        self._sync_ui_repair_result(classified, result, ok=ok)

    # ------------------------------------------------------------------
    # Step 1: Health classification (read-only, maintenance scope)
    # ------------------------------------------------------------------

    def _classify_health_signal(
        self, decision_proof: dict[str, Any]
    ) -> dict[str, Any]:
        """Classify a health signal from the information layer.

        Per A154: ``MAINTENANCE-SCOPE:health-only``.  The maintenance
        sovereign classifies the incoming health signal (identifies the
        error type, target file and diagnosis) without making the repair
        decision.  The repair decision is owned by the
        system-decision-sovereign (A152).
        """
        diagnosis = decision_proof.get("diagnosis") or {}
        return {
            "error_type": str(diagnosis.get("error_type") or ""),
            "target_file": str(diagnosis.get("file") or ""),
            "action": str(diagnosis.get("action") or ""),
            "diagnosis": diagnosis,
        }

    # ------------------------------------------------------------------
    # Step 4: Learning-store audit (E127: LEARNING:learning-system)
    # ------------------------------------------------------------------

    def _audit_repair_outcome(
        self,
        request: dict[str, Any],
        classified: dict[str, Any],
        result: dict[str, Any] | None,
        *,
        ok: bool,
    ) -> None:
        """Record the repair outcome in the learning store.

        Per E128: ``information-layer-status-event-audit``.  The outcome
        is recorded in the persistent learning store (E127:
        ``LEARNING:learning-system``) and the audit ledger.
        """
        # Record in the learning store.
        try:
            self.record_repair_outcome(
                error_class=str(classified.get("error_type") or "Unknown"),
                message=str(classified.get("reason") or ""),
                failure_code=str(request.get("failure_code") or ""),
                remedy="targeted-source-repair",
                ok=ok,
                file_path=str(classified.get("target_file") or ""),
                target_tool_id="main-system",
                run_id=str(request.get("request_id") or ""),
                detail={
                    "execution": (result or {}).get("execution", {}),
                    "verification": (result or {}).get("verification", {}),
                    "decision": (result or {}).get("decision", ""),
                    "request_id": str(request.get("request_id") or ""),
                },
            )
        except Exception:
            pass  # Best-effort.

    # ------------------------------------------------------------------
    # Step 5: UI sync
    # ------------------------------------------------------------------

    def _sync_ui_repair_result(
        self,
        classified: dict[str, Any],
        result: dict[str, Any],
        *,
        ok: bool,
    ) -> None:
        """Notify the UI of the repair outcome.

        Per E128: ``ui``.  The frontend is notified so it can refresh its
        status display.
        """
        try:
            asyncio.create_task(
                self._notify_ui(
                    "maintenance:repair-completed",
                    {
                        "ok": ok,
                        "target_file": str(classified.get("target_file") or ""),
                        "error_type": str(classified.get("error_type") or ""),
                        "decision": str(result.get("decision") or ""),
                        "verification": result.get("verification", {}),
                        "health_authority": self.ROLE,
                        "decision_authority": "system-decision-sovereign",
                    },
                )
            )
        except Exception:
            pass  # Best-effort.


__all__ = ["MaintenanceRepairChainMixin"]
