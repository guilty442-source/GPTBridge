"""Repair Decision Chain — A152/A154/E127/E128 governed repair decision
authority for the decision-sovereign.

Per the amended Governance Codex (H014, v1.73120), the repair decision
authority was moved from the maintenance sovereign (old A67/A72, now
superseded) to the decision-sovereign:

  * A152 (supersedes A67): ``REPAIR-DECISION:decision-sovereign``;
    ``FORBID:maintenance-owning-non-health-decisions``.
  * A154 (supersedes A72): ``MAINTENANCE-SCOPE:health-only``;
    ``FORBID:maintenance-code-change+maintenance-permission``.
  * E127 (supersedes E48): ``HEALTH:maintenance;
    REPAIR-DECISION:decision; RUNTIME-ACTION:system-runtime;
    CODE-ACTION:system-programming; LEARNING:learning-system``.
  * E128 (supersedes E52): ``ROUTE:health-signal>maintenance-classification
    >decision>permission>runtime-or-programming>executor>
    verification>information-layer>ui``.

The maintenance sovereign now performs **health classification** only
(monitor-system-health / preserve-system-health / maintain-system per
A125/E102).  The classified health signal is handed to the
decision-sovereign, which owns the repair **decision** and routes
it through the governed chain:

  decision > permission-validation > system-programming (code
  change) or system-runtime (runtime action) > governed-executor >
  independent-verification

This module is LOCAL CODE (same process as GPTBridgeApp).  It coordinates
existing in-process sovereigns (system-programming, permission) and
delegates the actual source mutation to the governed programming tool.
It never runs heavy work in the mother process.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class RepairDecisionChain:
    """A152/A154 governed repair decision chain for the
    decision-sovereign.

    The decision-sovereign is the SOLE repair decision authority
    (A152).  boot_core, watchdog, UI, and modules are signal-and-request
    only.  This chain receives a *classified* health signal from the
    maintenance sovereign, makes the repair decision, validates
    permissions, dispatches the governed executor (system-programming for
    code changes), independently verifies the result, and returns the
    outcome so the maintenance sovereign can record it in the learning
    store and sync the UI (information layer).
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def decide_and_route(self, classified_signal: dict[str, Any]) -> dict[str, Any]:
        """Make the repair decision and route through the governed chain.

        ``classified_signal`` is the health classification produced by the
        maintenance sovereign (error type, target file, diagnosis).  This
        method owns the repair DECISION (A152) — whether the fault is
        repairable under current policy — and the routing through
        permission validation and governed execution.
        """

        # ── Step 1: repair decision ──
        decision = self._decide_repairable(classified_signal)
        if not decision.get("repairable"):
            return {
                "ok": False,
                "decision": "denied-not-repairable",
                "reason": decision.get("reason", ""),
                **classified_signal,
            }

        # ── Step 2: permission validation ──
        permission = self._validate_permission(classified_signal)
        if not permission.get("authorized"):
            return {
                "ok": False,
                "decision": "denied-permission",
                "reason": permission.get("reason", ""),
                **classified_signal,
            }

        # ── Step 3: dispatch to system-programming (code change) ──
        execution = self._dispatch_to_programming(classified_signal, decision)

        # ── Step 4: independent verification ──
        verification = self._verify_independent(classified_signal, execution)

        ok = bool(verification.get("ok"))
        return {
            "ok": ok,
            "decision": "approved-repair-completed" if ok else "approved-repair-failed",
            "execution": execution,
            "verification": verification,
            **classified_signal,
        }

    # ------------------------------------------------------------------
    # Step 1: Repair decision
    # ------------------------------------------------------------------

    def _decide_repairable(
        self, classified_signal: dict[str, Any]
    ) -> dict[str, Any]:
        """Decide whether the classified fault is repairable under policy.

        Per A152: ``REPAIR-DECISION:decision-sovereign``.  The
        decision-sovereign analyses the classified health signal
        and decides whether a governed repair is permitted.
        """
        diagnosis = classified_signal.get("diagnosis") or {}
        action = str(diagnosis.get("action") or "")
        error_type = str(classified_signal.get("error_type") or "")

        # Only targeted (indentation/syntax family) errors are repairable.
        if action != "targeted":
            return {
                "repairable": False,
                "reason": f"action={action}; not targetable",
                "error_type": error_type,
            }

        # Dev mode: never repair source automatically.
        if os.environ.get("GPTBRIDGE_RENDERER_DEV_URL"):
            return {
                "repairable": False,
                "reason": "dev-mode; auto-repair disabled by policy",
                "error_type": error_type,
            }

        return {
            "repairable": True,
            "error_type": error_type,
            "diagnosis": diagnosis,
        }

    # ------------------------------------------------------------------
    # Step 2: Permission validation
    # ------------------------------------------------------------------

    def _validate_permission(
        self, classified_signal: dict[str, Any]
    ) -> dict[str, Any]:
        """Validate that the repair is permitted by governance.

        Per E128: ``decision>permission``.  The permission sovereign
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
                target=str(classified_signal.get("target_file") or ""),
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

    def _dispatch_to_programming(
        self,
        classified_signal: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Delegate the code change to the system-programming-sovereign.

        Per E127: ``CODE-ACTION:system-programming``.  The
        decision-sovereign makes the repair DECISION only; the
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

        target_file = str(classified_signal.get("target_file") or "")
        error_type = str(classified_signal.get("error_type") or "")

        try:
            result = programming_sovereign.request_tool_execution(
                requester_module="decision-sovereign",
                tool_id="main-system-source-repair",
                operation="targeted-indentation-repair",
                payload={
                    "target_file": target_file,
                    "error_type": error_type,
                    "diagnosis": decision.get("diagnosis") or {},
                    "authority": "decision-sovereign",
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

    def _verify_independent(
        self,
        classified_signal: dict[str, Any],
        execution: dict[str, Any],
    ) -> dict[str, Any]:
        """Independently verify the repair result.

        Per E128: ``executor>verification``.  This step is independent
        from the executor: it re-compiles the target file from disk
        rather than trusting the executor's self-report.
        """
        target_file = str(classified_signal.get("target_file") or "")

        if not execution.get("ok"):
            return {"ok": False, "reason": "execution-failed"}

        if execution.get("skipped"):
            return {"ok": False, "reason": f"skipped: {execution.get('reason')}"}

        # Independent re-compile: read the file from disk and compile it.
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


__all__ = ["RepairDecisionChain"]
