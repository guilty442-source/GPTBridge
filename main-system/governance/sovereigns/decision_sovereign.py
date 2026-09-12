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
  * language-review-sub-sovereign          -- programming-language conformance
  * dependency-sync-sub-sovereign          -- third-party software management
  * learning-evidence-sync-sub-sovereign   -- persistent error learning
  * release-update-sync-sub-sovereign      -- governed code-change dispatch

All are LOCAL CODE (same process as GPTBridgeApp) and coordinate existing
in-process services; they never run heavy work in this mother process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX
from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome
from core_system.governance_rule_coordination import GovernanceRuleCoordination
from core_system.repair_decision_chain import RepairDecisionChain
from core_system.sovereign_utils import _iso_now


_DECISION_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "decision"),
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
    "language_review_sovereign": "language-review-sub-sovereign",
    "third_party_sovereign": "dependency-sync-sub-sovereign",
    "learning_system_sovereign": "learning-evidence-sync-sub-sovereign",
    "system_programming_sovereign": "release-update-sync-sub-sovereign",
}


class DecisionSovereign(SovereignBase):
    """決策主宰：啟動堆疊裁決/派工、維修決策、子主宰擁有者（不執行）。

    Sovereign-stack startup dispatcher (A64) and platform sub-sovereign
    owner.  Per A63 this sovereign holds decision power only; materialization
    and activation are executed by the governed ``SovereignStackExecutor``.
    """

    sovereign_id = "decision-sovereign"

    ROLE = _DECISION_SOVEREIGN.id

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
        from governance.registries import parent_of

        app = self.app
        return {
            "decision-sovereign": self,
            "permission-sovereign": getattr(app, "permission_sovereign", None),
            "synchronization-sovereign": getattr(
                app, "synchronization_sovereign", None
            ),
            "system-runtime-sovereign": getattr(
                app, "system_runtime_sovereign", None
            ),
        }.get(parent_of(child_id))

    def _child(self, child_id: str) -> Any:
        """Resolve a child through its codex-registered parent's registry."""
        parent = self._parent_for(child_id)
        if parent is None:
            return None
        return getattr(parent, "_sub_sovereigns", {}).get(child_id)

    def _all_children(self) -> dict[str, Any]:
        """All materialized children across every parent's registry."""
        merged: dict[str, Any] = {}
        for parent in (
            self,
            getattr(self.app, "permission_sovereign", None),
            getattr(self.app, "synchronization_sovereign", None),
            getattr(self.app, "system_runtime_sovereign", None),
        ):
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
    # Single-gate adjudication (A10/A11)
    # ------------------------------------------------------------------

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：啟動協調、維修路由、子主宰指派。"""
        intent = request.intent

        if intent == "startup.stack.dispatch":
            return await self._adjudicate_startup_dispatch(request)
        if intent == "repair.decide-and-route":
            return await self._adjudicate_repair_decision(request)
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
        """A152/A154/E127/E128: 維修決策鏈。"""
        classified_signal = request.payload.get("classified_signal")
        if not classified_signal:
            return refusal_outcome("MISSING_CLASSIFIED_SIGNAL", self.verified_basis("A152"))

        repair_type = classified_signal.get("repair_type", "unknown")

        return accepted_outcome(
            {
                "repair_decision": "authorized",
                "repair_type": repair_type,
                "route": "permission-validation > governed-executor > verification",
                "forbidden": "maintenance-owning-non-health-decisions",
            },
            self.verified_basis("A152", "A154", "E127", "E128"),
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
        """法典規則協調（A63）。"""
        return accepted_outcome(
            {"coordination": "governance-rules-aligned", "source": "codex-only"},
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
        executor = getattr(self.app, "sovereign_stack_executor", None)
        if executor is not None:
            await executor.deactivate(self)
        self._save_state({"stopped_at": _iso_now()})
        await super().stop()

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
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
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
            "sub_sovereigns": [
                self._child_status("runtime-state-sync-sub-sovereign"),
                self._child_status("resource-dependency-sync-sub-sovereign"),
                self._child_status("data-governance-sub-sovereign"),
                self._child_status("channel-contract-sync-sub-sovereign"),
                self._child_status("language-review-sub-sovereign"),
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
            "runtime": self._child_status("runtime-state-sync-sub-sovereign"),
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
            "resource": self._child_status("resource-dependency-sync-sub-sovereign"),
            "data": self._child_status("data-governance-sub-sovereign"),
            "integration": self._child_status("channel-contract-sync-sub-sovereign"),
            "language_review": self._child_status("language-review-sub-sovereign"),
            "third_party": self._child_status("dependency-sync-sub-sovereign"),
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

        maintenance_sovereign = getattr(self.app, "maintenance_sovereign", None)
        permission_sovereign = getattr(self.app, "permission_sovereign", None)
        return {
            "state": "delegated",
            "owner": self.module_id,
            "sub_sovereigns": [
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
                    "language-review-sub-sovereign", "orchestration_status"
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
            "runtime": self._child_status(
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
            "resource": self._child_status(
                "resource-dependency-sync-sub-sovereign", "orchestration_status"
            ),
            "data": self._child_status(
                "data-governance-sub-sovereign", "orchestration_status"
            ),
            "integration": self._child_status(
                "channel-contract-sync-sub-sovereign", "orchestration_status"
            ),
            "language_review": self._child_status(
                "language-review-sub-sovereign", "orchestration_status"
            ),
            "third_party": self._child_status(
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
                    "language-review-sub-sovereign", "orchestration_status"
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
