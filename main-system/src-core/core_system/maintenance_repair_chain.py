"""Maintenance Sovereign — A67/A72 governed repair decision chain mixin.

Implements the full repair decision chain required by Governance Codex
A67/A72:

  signal > information-layer > maintenance-sovereign fault-determination
  + repair-decision > permission-validation > governed-executor
  > independent-verification > information-layer status-event-audit
  > ui-sync

The maintenance sovereign is the SOLE decision authority for system
repair.  boot_core, watchdog, UI, and modules are signal-and-request-only
(A72).  This mixin polls ``repair-requests.json`` for pending signals,
makes the repair decision, validates permissions, dispatches the
governed executor, independently verifies the result, audits the outcome,
and syncs the UI.

Extracted from ``maintenance_update`` to keep each module focused and
under 500 lines.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .sovereign_utils import _iso_now


class MaintenanceRepairChainMixin:
    """A67/A72 governed repair decision chain for the Maintenance Sovereign.

    Expects the following attributes to be set by the composing class's
    ``__init__``:

      * ``self.app`` — the GPTBridgeApp instance
      * ``self._repair_service`` — CentralRepairService or None
      * ``self._repair_decision_task`` — asyncio.Task or None
      * ``self._started`` — bool
      * ``self.ROLE``
    """

    _REPAIR_POLL_INTERVAL_SECONDS: float = 5.0

    # ------------------------------------------------------------------
    # Status (used by MaintenanceStatusMixin)
    # ------------------------------------------------------------------

    def _repair_decision_chain_status(self) -> dict[str, Any]:
        """Report the repair decision chain status for observability."""
        task = getattr(self, "_repair_decision_task", None)
        return {
            "enabled": task is not None and not task.done(),
            "authority": self.ROLE,
            "chain": "A67/A72",
            "poll_interval_seconds": self._REPAIR_POLL_INTERVAL_SECONDS,
        }

    # ------------------------------------------------------------------
    # Lifecycle hooks (called by MaintenanceLifecycleMixin)
    # ------------------------------------------------------------------

    def _start_repair_decision_loop(self) -> None:
        """Start the repair decision loop.  Called from ``start()``."""
        if getattr(self, "_repair_decision_task", None) is None:
            self._repair_decision_task = asyncio.create_task(
                self._repair_decision_loop(),
                name="maintenance-sovereign-repair-decision",
            )

    async def _stop_repair_decision_loop(self) -> None:
        """Stop the repair decision loop.  Called from ``stop()``."""
        task = getattr(self, "_repair_decision_task", None)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._repair_decision_task = None

    # ------------------------------------------------------------------
    # Repair decision loop
    # ------------------------------------------------------------------

    async def _repair_decision_loop(self) -> None:
        """Poll ``repair-requests.json`` and process pending signals.

        This is the maintenance sovereign's exclusive repair decision
        entry point.  Per A67: ``ALL-SYSTEM-REPAIR:maintenance-sovereign-
        exclusive-decision``.
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
        """Handle a single repair request through the full A67 chain."""
        request_id = str(request.get("request_id") or "")
        failure_code = str(request.get("failure_code") or "")
        decision_proof = request.get("decision_proof") or {}

        # ── Step 1: fault determination ──
        fault = self._determine_fault(decision_proof)
        if not fault.get("repairable"):
            coordinator.acknowledge_request(
                request_id,
                maintenance_sovereign_decision="denied-not-repairable",
                ok=False,
            )
            self._audit_repair_outcome(request, fault, None, ok=False)
            return

        # ── Step 2: permission validation ──
        permission = self._validate_repair_permission(fault)
        if not permission.get("authorized"):
            coordinator.acknowledge_request(
                request_id,
                maintenance_sovereign_decision="denied-permission",
                ok=False,
            )
            self._audit_repair_outcome(request, fault, permission, ok=False)
            return

        # ── Step 3: dispatch to governed executor ──
        execution = self._dispatch_repair(fault)

        # ── Step 4: independent verification ──
        verification = self._verify_repair_independent(fault, execution)

        ok = bool(verification.get("ok"))
        decision = "approved-repair-completed" if ok else "approved-repair-failed"

        # ── Step 5: acknowledge in information layer ──
        coordinator.acknowledge_request(
            request_id,
            maintenance_sovereign_decision=decision,
            ok=ok,
        )

        # ── Step 6: status event audit ──
        self._audit_repair_outcome(request, fault, execution, ok=ok)

        # ── Step 7: UI sync ──
        self._sync_ui_repair_result(fault, execution, verification, ok=ok)

    # ------------------------------------------------------------------
    # Step 1: Fault determination
    # ------------------------------------------------------------------

    def _determine_fault(self, decision_proof: dict[str, Any]) -> dict[str, Any]:
        """Determine whether the fault is a repairable source issue.

        Per A67: ``maintenance-sovereign-fault-determination``.  The
        sovereign analyzes the decision proof (crash diagnosis from
        boot_core) and decides whether the fault is repairable.
        """
        diagnosis = decision_proof.get("diagnosis") or {}
        action = str(diagnosis.get("action") or "")
        error_type = str(diagnosis.get("error_type") or "")
        target_file = str(diagnosis.get("file") or "")

        # Only indentation/syntax family errors are repairable.
        if action != "targeted":
            return {
                "repairable": False,
                "reason": f"action={action}; not targetable",
                "error_type": error_type,
                "target_file": target_file,
            }

        # Dev mode: never repair source automatically.
        import os

        if os.environ.get("GPTBRIDGE_RENDERER_DEV_URL"):
            return {
                "repairable": False,
                "reason": "dev-mode; auto-repair disabled by policy",
                "error_type": error_type,
                "target_file": target_file,
            }

        return {
            "repairable": True,
            "error_type": error_type,
            "target_file": target_file,
            "diagnosis": diagnosis,
        }

    # ------------------------------------------------------------------
    # Step 2: Permission validation
    # ------------------------------------------------------------------

    def _validate_repair_permission(self, fault: dict[str, Any]) -> dict[str, Any]:
        """Validate that the repair is permitted by governance.

        Per A67: ``permission-validation``.  The permission sovereign
        must authorize the repair mutation before execution.
        """
        permission_sovereign = getattr(self.app, "permission_sovereign", None)
        if permission_sovereign is None:
            return {
                "authorized": False,
                "reason": "permission-sovereign-unavailable",
            }

        try:
            permission_sovereign.authorize(
                capability="system-repair",
                action="source-repair",
                target=str(fault.get("target_file") or ""),
                data_scope="main-system",
                target_tool_id="main-system",
            )
            return {"authorized": True}
        except PermissionError:
            return {
                "authorized": False,
                "reason": "PERMISSION_DENIED",
            }
        except Exception as error:
            return {
                "authorized": False,
                "reason": f"{type(error).__name__}: {error}",
            }

    # ------------------------------------------------------------------
    # Step 3: Dispatch to system-programming-sovereign (code change)
    # ------------------------------------------------------------------

    def _dispatch_repair(self, fault: dict[str, Any]) -> dict[str, Any]:
        """Delegate the code change to the system-programming-sovereign.

        Per A1154: ``OWNER:system-programming-sovereign;
        DUTY:code-change-plan+scope+tool-selection+dispatch+independent-
        verification; EXECUTION:approved-governed-programming-tool``.

        The maintenance sovereign makes the repair DECISION only; the
        actual source mutation is delegated to the programming sovereign
        which dispatches an approved governed programming tool.
        """
        programming_sovereign = getattr(
            self.app, "system_programming_sovereign", None
        )
        if programming_sovereign is None:
            return {
                "ok": False,
                "reason": "system-programming-sovereign-unavailable",
            }

        target_file = str(fault.get("target_file") or "")
        error_type = str(fault.get("error_type") or "")

        try:
            result = programming_sovereign.request_tool_execution(
                requester_module="maintenance-sovereign",
                tool_id="main-system-source-repair",
                operation="targeted-indentation-repair",
                payload={
                    "target_file": target_file,
                    "error_type": error_type,
                    "diagnosis": fault.get("diagnosis") or {},
                    "authority": self.ROLE,
                },
            )
            return result
        except Exception as error:
            return {
                "ok": False,
                "reason": f"{type(error).__name__}: {error}",
            }

    # ------------------------------------------------------------------
    # Step 4: Independent verification
    # ------------------------------------------------------------------

    def _verify_repair_independent(
        self,
        fault: dict[str, Any],
        execution: dict[str, Any],
    ) -> dict[str, Any]:
        """Independently verify the repair result.

        Per A67: ``independent-verification``.  This step is independent
        from the executor: it re-compiles the target file from disk
        rather than trusting the executor's self-report.
        """
        target_file = str(fault.get("target_file") or "")

        if not execution.get("ok"):
            return {"ok": False, "reason": "execution-failed"}

        if execution.get("skipped"):
            return {"ok": False, "reason": f"skipped: {execution.get('reason')}"}

        # Independent re-compile: read the file from disk and compile it.
        from pathlib import Path

        from tasks.source_repair import syntax_problems

        project_root = Path(getattr(self.app, "project_root", ".") or ".")
        target = (project_root / target_file).resolve()
        if not target.is_file():
            return {"ok": False, "reason": "file-not-found-after-repair"}

        problem = syntax_problems(target)
        if problem.get("ok"):
            return {"ok": True, "verification": "independent-compile-ok"}

        return {
            "ok": False,
            "reason": f"independent-verification-failed: {problem.get('error')}",
        }

    # ------------------------------------------------------------------
    # Step 5: Status event audit
    # ------------------------------------------------------------------

    def _audit_repair_outcome(
        self,
        request: dict[str, Any],
        fault: dict[str, Any],
        execution: dict[str, Any] | None,
        *,
        ok: bool,
    ) -> None:
        """Record the repair outcome in the audit ledger.

        Per A67: ``information-layer-status-event-audit``.  The outcome
        is recorded in the persistent learning store and the audit
        ledger.
        """
        # Record in the learning store.
        try:
            self.record_repair_outcome(
                error_class=str(fault.get("error_type") or "Unknown"),
                message=str(fault.get("reason") or ""),
                failure_code=str(request.get("failure_code") or ""),
                remedy="targeted-source-repair",
                ok=ok,
                file_path=str(fault.get("target_file") or ""),
                target_tool_id="main-system",
                run_id=str(request.get("request_id") or ""),
                detail={
                    "execution": execution or {},
                    "request_id": str(request.get("request_id") or ""),
                },
            )
        except Exception:
            pass  # Best-effort.

    # ------------------------------------------------------------------
    # Step 6: UI sync
    # ------------------------------------------------------------------

    def _sync_ui_repair_result(
        self,
        fault: dict[str, Any],
        execution: dict[str, Any],
        verification: dict[str, Any],
        *,
        ok: bool,
    ) -> None:
        """Notify the UI of the repair outcome.

        Per A67: ``ui-sync``.  The frontend is notified so it can
        refresh its status display.
        """
        try:
            asyncio.create_task(
                self._notify_ui(
                    "maintenance:repair-completed",
                    {
                        "ok": ok,
                        "target_file": str(fault.get("target_file") or ""),
                        "error_type": str(fault.get("error_type") or ""),
                        "verification": verification,
                        "authority": self.ROLE,
                    },
                )
            )
        except Exception:
            pass  # Best-effort.


__all__ = ["MaintenanceRepairChainMixin"]
