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

    def decide_and_route(
        self,
        classified_signal: dict[str, Any],
        *,
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """Make the repair decision and route through the governed chain.

        ``classified_signal`` is the health classification produced by the
        maintenance sovereign (error type, target file, diagnosis).  This
        method owns the repair DECISION (A152) — whether the fault is
        repairable under current policy — and the routing through
        permission validation and governed execution.

        Autonomous repair (governor directive 2026-09-18): the A366
        per-item user-confirmation gate is retired for both tiers —
        ``targeted`` (mutation) and ``runtime-recovery`` (stability)
        repairs proceed through the governed chain and are registered
        with the change-acceptance sub-sovereign as system-audit intake
        before dispatch; acceptance is recorded with the verification
        result.  ``user_confirmed`` remains accepted so explicit one-time
        manual commands stay lawful within their scope.
        """
        tier = self._repair_tier(classified_signal)

        # Governor directive (2026-09-18): the per-item user-confirmation
        # gate is retired — autonomous repairs execute through this chain
        # under the system-audit flow (change-acceptance intake before
        # dispatch, acceptance recorded with the verification result).

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

        # ── Step 3: system-audit intake (change-acceptance) ──
        audit_change_id = str(
            classified_signal.get("request_id")
            or classified_signal.get("fault_id")
            or ""
        )
        acceptance = self._change_acceptance()
        audit_intake = "skipped-unavailable"
        if acceptance is not None and audit_change_id:
            submitted = acceptance.submit_change(
                audit_change_id,
                {
                    "kind": "autonomous-repair",
                    "tier": tier,
                    "failure_code": str(
                        classified_signal.get("failure_code") or ""
                    ),
                    "action": str(classified_signal.get("action") or ""),
                    "tool_id": str(classified_signal.get("tool_id") or ""),
                    "target_file": str(
                        classified_signal.get("target_file") or ""
                    ),
                    "decision": decision,
                    "permission": permission,
                },
            )
            if not submitted:
                return {
                    "ok": False,
                    "decision": "denied-audit-conflict",
                    "reason": (
                        "change-acceptance refused the repair submission "
                        "(conflicting or malformed change spec)"
                    ),
                    **classified_signal,
                }
            audit_intake = "submitted"

        # ── Step 4: dispatch (E127: runtime action vs code change) ──
        if tier == "stability":
            execution = self._dispatch_to_runtime(classified_signal, decision)
        else:
            execution = self._dispatch_to_programming(classified_signal, decision)

        # ── Step 5: independent verification ──
        verification = self._verify_independent(classified_signal, execution)

        ok = bool(verification.get("ok"))
        audit_accepted = False
        if audit_intake == "submitted":
            audit_accepted = bool(
                acceptance.accept_change(
                    audit_change_id,
                    {
                        "ok": ok,
                        "execution": execution,
                        "verification": verification,
                    },
                )
            )
        return {
            "ok": ok,
            "decision": "approved-repair-completed" if ok else "approved-repair-failed",
            "execution": execution,
            "verification": verification,
            "audit": {
                "flow": "system-audit",
                "change_id": audit_change_id,
                "intake": audit_intake,
                "accepted": audit_accepted,
            },
            **classified_signal,
        }

    def _change_acceptance(self) -> Any:
        """Locate the change-acceptance sub-sovereign (system-audit intake).

        Autonomous repairs register as changes before dispatch and record
        acceptance with the verification result — the audit trail lives on
        the sovereign, not on a user-confirmation queue.
        """
        app = getattr(self, "app", None)
        holders = (
            getattr(app, "_sub_sovereigns", None),
            getattr(
                getattr(app, "decision_sovereign", None),
                "_sub_sovereigns",
                None,
            ),
        )
        for holder in holders:
            if isinstance(holder, dict):
                sovereign = holder.get("change-acceptance-sub-sovereign")
                if sovereign is not None:
                    return sovereign
        return None

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

        # Repairable classes: ``targeted`` (indentation/syntax family,
        # mutation tier — A366 confirmation-gated) and ``runtime-recovery``
        # (stability tier — non-mutating governed recovery).
        if action not in ("targeted", "runtime-recovery"):
            return {
                "repairable": False,
                "reason": f"action={action}; not repairable",
                "error_type": error_type,
            }

        # Dev mode: never repair automatically.
        if os.environ.get("GPTBRIDGE_RENDERER_DEV_URL"):
            return {
                "repairable": False,
                "reason": "dev-mode; auto-repair disabled by policy",
                "error_type": error_type,
            }

        if action == "runtime-recovery" and not str(
            diagnosis.get("tool_id") or ""
        ):
            return {
                "repairable": False,
                "reason": "runtime-recovery requires diagnosis.tool_id",
                "error_type": error_type,
            }

        learned = self._learned_remedy(classified_signal)
        if learned.get("ineffective"):
            return {
                "repairable": False,
                "reason": (
                    "learned-ineffective-remedy; escalate for "
                    "user-confirmed alternative"
                ),
                "error_type": error_type,
                "learned": learned,
            }

        return {
            "repairable": True,
            "error_type": error_type,
            "diagnosis": diagnosis,
            "repair_tier": self._repair_tier(classified_signal),
            "learned": learned,
        }

    # ------------------------------------------------------------------
    # Repair tier + learned-remedy consultation
    # ------------------------------------------------------------------

    @staticmethod
    def _repair_tier(classified_signal: dict[str, Any]) -> str:
        """Classify the repair into ``mutation`` or ``stability`` tier.

        ``mutation`` (``action == "targeted"``) changes source code and is
        A366 confirmation-gated.  ``stability`` (``action ==
        "runtime-recovery"``) is non-mutating governed recovery — owned-
        database inspection plus artifact rebuild — the sanctioned
        ``automatic_repair`` scope (restore-system-stability-only).
        """
        diagnosis = classified_signal.get("diagnosis") or {}
        if str(diagnosis.get("action") or "") == "runtime-recovery":
            return "stability"
        return "mutation"

    def _learned_remedy(self, classified_signal: dict[str, Any]) -> dict[str, Any]:
        """Consult the repair-learning store for this fault signature.

        The learning loop closes here: promoted error→remedy patterns are
        consulted before dispatch, and a remedy that repeatedly failed for
        this exact signature is suppressed so the chain escalates instead
        of burning retries on a proven-ineffective action.
        """
        empty: dict[str, Any] = {"suggested": False, "ineffective": False}
        maintenance = getattr(self.app, "maintenance_sovereign", None)
        suggest = getattr(maintenance, "suggest_remedy", None)
        if suggest is None:
            return empty
        error_type = str(classified_signal.get("error_type") or "Unknown")
        target_file = str(classified_signal.get("target_file") or "")
        try:
            suggestion = suggest(
                error_class=error_type, message="", file_path=target_file
            )
        except Exception:
            return empty
        # ``suggest_remedy`` attaches ``_learning_store``/``_learner``
        # lazily; read them after the call.
        store = getattr(maintenance, "_learning_store", None)
        result: dict[str, Any] = {"suggested": False, "ineffective": False}
        if isinstance(suggestion, dict) and suggestion.get("suggested"):
            result.update(
                {
                    "suggested": True,
                    "remedy": str(suggestion.get("remedy") or ""),
                    "success_rate": suggestion.get("success_rate"),
                    "occurrence_count": suggestion.get("occurrence_count"),
                }
            )
        # Suppression: the same signature's most recent outcomes all failed
        # with the remedy this tier would apply — the remedy is learned to
        # be ineffective for this fault, so escalate rather than retry.
        outcomes: list[dict[str, Any]] = []
        if store is not None:
            try:
                from tasks.repair_learning import _normalize_error_signature

                signature_hash = _normalize_error_signature(
                    error_type, "", file_path=target_file
                )
                outcomes = store.get_outcomes_for_signature(
                    signature_hash, limit=3
                )
            except Exception:
                outcomes = []
        if len(outcomes) >= 2 and all(not o.get("ok") for o in outcomes):
            result["ineffective"] = True
            result["suppressed_remedy"] = str(outcomes[0].get("remedy") or "")
        return result

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

        diagnosis = classified_signal.get("diagnosis") or {}
        if self._repair_tier(classified_signal) == "stability":
            tool_id = str(diagnosis.get("tool_id") or "")
            capability = "system-repair"
            action = "runtime-recovery"
            target = tool_id
            data_scope = f"{tool_id}/runtime/"
            target_tool_id = tool_id or "main-system"
        else:
            capability = "system-repair"
            action = "source-repair"
            target = str(classified_signal.get("target_file") or "")
            data_scope = "main-system"
            target_tool_id = "main-system"

        try:
            permission_sovereign.authorize(
                capability=capability,
                action=action,
                target=target,
                data_scope=data_scope,
                target_tool_id=target_tool_id,
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
    # Step 3: Dispatch to release-update-sync-sub-sovereign (code change)
    # ------------------------------------------------------------------

    def _dispatch_to_programming(
        self,
        classified_signal: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Delegate the code change to the release-update-sync-sub-sovereign.

        Per E127: ``CODE-ACTION:release-update-sync``.  The
        decision-sovereign makes the repair DECISION only; the
        actual source mutation is delegated to the release-update
        synchronization sub-sovereign which dispatches an approved
        governed programming tool.
        """
        programming_sovereign = getattr(
            self.app, "system_programming_sovereign", None
        )
        if programming_sovereign is None:
            return {
                "ok": False,
                "reason": "release-update-sync-sub-sovereign-unavailable",
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
    # Step 3b: Dispatch to runtime recovery (stability tier, no mutation)
    # ------------------------------------------------------------------

    def _dispatch_to_runtime(
        self,
        classified_signal: dict[str, Any],
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Delegate non-mutating runtime recovery to the governed executor.

        Per E127: ``RUNTIME-ACTION:system-runtime`` and the
        ``automatic_repair`` policy (restore-system-stability-only,
        ``main-system-executes-central-repair-with-target-isolated-
        database-only``).  The recovery is the governed central-repair
        path used by the start-failure flow: owned-database inspection
        and preservation plus, when a verified recipe applies, an
        artifact rebuild through the governed package rebuilder.  No
        source code is mutated here.
        """
        diagnosis = classified_signal.get("diagnosis") or {}
        tool_id = str(diagnosis.get("tool_id") or "")
        project_root = Path(getattr(self.app, "project_root", ".") or ".")
        failure_code = str(
            classified_signal.get("failure_code") or "TOOL_RUNTIME_CRASH"
        )

        try:
            from tasks.central_repair import CentralRepairService
            from tasks.package_rebuilder import ToolPackageRebuilder

            repair_root = project_root / "main-system" / "data" / "automatic-repair"
            repair_root.mkdir(parents=True, exist_ok=True)
            service = CentralRepairService(project_root, repair_root)
            rebuilder = ToolPackageRebuilder(project_root, project_root / "main-system")
            result = service.repair_tool(
                tool_id,
                failure_code,
                package_rebuilder=rebuilder.rebuild,
            )
            result["dispatch"] = "system-runtime"
            result["learned"] = decision.get("learned") or {}
            return result
        except Exception as error:
            return {
                "ok": False,
                "dispatch": "system-runtime",
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

        if self._repair_tier(classified_signal) == "stability":
            # Runtime recovery is verified by the governed executor's own
            # bounded workflow verdict plus the surviving audit evidence:
            # the repair ran its inspection/rebuild steps under a
            # permission-validated scope and reported a completed or
            # evidence-only workflow.  A report of errors fails closed.
            if execution.get("errors"):
                return {
                    "ok": False,
                    "reason": f"runtime-recovery-errors: {execution.get('errors')}",
                }
            return {
                "ok": True,
                "verification": "runtime-recovery-verified",
                "workflow_status": execution.get("workflow_status"),
            }

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
