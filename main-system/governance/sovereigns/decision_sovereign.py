"""Decision Sovereign — 決策主宰（最高決策權，啟動協調、維修決策、子主宰管理）。

法典依據:
- sovereign_id: decision-sovereign (position 2)
- area: decision
- rank: top-decision-sovereign
- basis: codex
- duties: startup-stack-dispatch|repair-decision-owner|sub-sovereign-owner|governance-rule-coordination
- powers: adjudicate-repair|dispatch-sub-sovereigns|coordinate-governance-rules
- prohibitions: FORBID:direct-execution|FORBID:own-health-decisions (A152/A154)

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
sovereign stack startup to this sovereign via ``start_sovereign_stack``.

The decision-sovereign also owns the repair DECISION chain per
A152/A154/E127/E128: the health-maintenance-test sub-sovereign classifies
health signals (health-only scope) and hands them to this sovereign, which
makes the repair decision, validates permissions, and routes to the
release-update (code change) or runtime-state (runtime action)
synchronization chain for governed execution.

Owned in-process sub-sovereigns (codex-aligned identities, A302–A323):
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

import asyncio
import json
import os
import time
from importlib import import_module
from pathlib import Path
from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX
from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome
from core_system.governance_rule_coordination import GovernanceRuleCoordination
from core_system.repair_decision_chain import RepairDecisionChain
from core_system.sovereign_utils import _iso_now


def _sub_sovereigns_module() -> Any:
    """Load the hyphenated ``governance.sub-sovereigns`` package lazily.

    The sub-sovereign layer lives under ``governance.sub_sovereigns`` and is
    resolved through importlib to keep this module import-safe while the
    governance package is still initializing.
    """

    return import_module("governance.sub_sovereigns")


_DECISION_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "decision"),
    None,
)
if _DECISION_SOVEREIGN is None:
    raise RuntimeError("decision sovereign not found in Governance Codex")

DECISION_SOVEREIGN_RESPONSIBILITIES = _DECISION_SOVEREIGN.duties


class DecisionSovereign(SovereignBase):
    """決策主宰：啟動堆疊協調、維修決策、子主宰擁有者。

    Sovereign-stack startup dispatcher (A64) and platform sub-sovereign
    owner.  Responsibilities at startup:

      - Receive the dispatch from GPTBridgeApp to materialize the entire
        sovereign stack in order (health-maintenance, permission,
        self-maintenance, then the Decision Sovereign's sub-sovereigns)
      - Consume the validated dependency state (env var + orchestrator report)
      - Record the sovereign startup phase into the platform startup status
      - Start its own sub-sovereigns: Runtime State, Resource Dependency,
        Data Governance, Channel Contract, Language Review, Dependency,
        Learning Evidence, Release Update
      - Coordinate (read-only) the Health Maintenance Test Sub-Sovereign and
        Permission Sovereign after this sovereign has materialized them
      - Delegate all execution to governed executors (never in this process)
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
        # Sub-sovereigns owned and started by the Decision Sovereign.
        # The attribute names are kept stable for existing callers; the
        # classes are the codex-aligned governance-layer identities
        # (A302–A310/A322/A323/A327).
        sub = _sub_sovereigns_module()
        self.runtime_sovereign = sub.RuntimeStateSyncSubSovereign(app, parent=self)
        self.resource_sovereign = sub.ResourceDependencySyncSubSovereign(app, parent=self)
        self.data_sovereign = sub.DataGovernanceSubSovereign(app, parent=self)
        self.integration_sovereign = sub.ChannelContractSyncSubSovereign(app, parent=self)
        self.language_review_sovereign = sub.LanguageReviewSubSovereign(app, parent=self)
        self.third_party_sovereign = sub.DependencySyncSubSovereign(app, parent=self)
        self.learning_system_sovereign = sub.LearningEvidenceSyncSubSovereign(app, parent=self)
        self.system_programming_sovereign = sub.ReleaseUpdateSyncSubSovereign(app, parent=self)
        self._sub_sovereigns: dict[str, Any] = {
            child.sovereign_id: child
            for child in (
                self.runtime_sovereign,
                self.resource_sovereign,
                self.data_sovereign,
                self.integration_sovereign,
                self.language_review_sovereign,
                self.third_party_sovereign,
                self.learning_system_sovereign,
                self.system_programming_sovereign,
            )
        }
        self.governance_rule_coordination = GovernanceRuleCoordination(app)
        # A152/A154/E127/E128: the decision-sovereign owns the repair
        # decision chain.  The health-maintenance sub-sovereign classifies
        # health signals (health-only) and delegates the decision here.
        self._repair_decision_chain = RepairDecisionChain(app)

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
            return refusal_outcome("INVALID_DEPENDENCY_STATE", self.verified_basis("A64", "A130"))

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
            self.verified_basis("A64", "A128", "A130", "A155"),
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

        if sub_sovereign not in set(self._sub_sovereigns) | {
            "health-maintenance-test-sub-sovereign",
            "policy-architecture-sub-sovereign",
            "priority-capability-sub-sovereign",
            "change-acceptance-sub-sovereign",
        }:
            return refusal_outcome("UNKNOWN_SUB_SOVEREIGN", self.verified_basis("A64", "A323"))

        return accepted_outcome(
            {
                "sub_sovereign": sub_sovereign,
                "action": action,
                "authority": "parent-decision-sovereign",
                "execution": "delegated-to-governed-executor",
            },
            self.verified_basis("A64", "A284", "A287", "A323"),
        )

    async def _adjudicate_governance_coordination(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """法典規則協調（A63）。"""
        return accepted_outcome(
            {"coordination": "governance-rules-aligned", "source": "codex-only"},
            self.verified_basis("A12", "A63"),
        )

    def register_sub_sovereign(self, name: str, sovereign: Any) -> None:
        self._sub_sovereigns[name] = sovereign

    def get_sub_sovereign(self, name: str) -> Any | None:
        return self._sub_sovereigns.get(name)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Mark the Decision Sovereign active and register its sub-sovereigns.

        The actual sub-sovereign startup is performed by
        ``start_sovereign_stack`` (called by the startup executor at the
        certified phase boundary); this activation is the decision-layer
        surface only.
        """
        state = await super().start()
        # Surface the owned children on the app registry for the status API.
        app_registry = getattr(self.app, "_sub_sovereigns", None)
        if isinstance(app_registry, dict):
            app_registry.update(self._sub_sovereigns)
        state["sub_sovereigns"] = list(self._sub_sovereigns.keys())
        return state

    async def start_sovereign_stack(self) -> bool:
        """Governed-executor startup of the entire sovereign stack.

        Per A63 (sovereigns are decision-only) and A64 (sub-sovereigns
        control/dispatch under parent authority), the mother process
        (GPTBridgeApp) must not directly materialize sovereigns.  Instead,
        it dispatches to this sovereign, which materializes the top-level
        sovereigns in order and then starts the Decision Sovereign's own
        sub-sovereigns.  All work remains delegated to governed executors.

        Order (E155 bounded-independent-parallelism within each step):
          1. Synchronization child sub-sovereigns + daily cleaner (learning
             evidence, release update, cleaner — independent, started in
             parallel)
          2. Permission Sovereign (read-only coordination surface)
          3. Health Maintenance Test Sub-Sovereign + main-system
             self-maintenance (independent — started in parallel)
          4. Decision Sovereign and its six sub-sovereigns

        Returns the maintenance_ready flag for the final readiness log.
        """

        app = self.app
        sub = _sub_sovereigns_module()
        # Step timings feed the startup executor's phase evidence so a
        # deadline breach reports the exact bottleneck (E173).
        step_timings: dict[str, int] = {}
        _step_start = time.monotonic()

        # Materialize the health-maintenance sub-sovereign if the app has
        # not already provided one (A302/A323: child-of-decision-sovereign).
        if getattr(app, "maintenance_sovereign", None) is None:
            app.maintenance_sovereign = sub.HealthMaintenanceTestSubSovereign(
                app, parent=self
            )

        # Learning-evidence and release-update are synchronization child
        # sub-sovereigns. They start before maintenance so every subsequent
        # failure and repair can be learned, and every code change has one
        # governed dispatch owner.
        # The daily cleaner is independent — E155 bounded-independent-
        # parallelism applies to these three starts.
        app.learning_system_sovereign = self.learning_system_sovereign
        app.system_programming_sovereign = self.system_programming_sovereign
        await asyncio.gather(
            self.learning_system_sovereign.start(),
            self.system_programming_sovereign.start(),
            app.daily_global_cleaner_service.start(),
        )
        step_timings["peer-sovereigns-and-cleaner_ms"] = int(
            (time.monotonic() - _step_start) * 1000
        )
        _step_start = time.monotonic()

        # 1. Health Maintenance Test Sub-Sovereign — periodic maintenance,
        #    health, repair classification
        app._mark_startup_phase("maintenance_sovereign_starting")

        async def _start_maintenance() -> None:
            try:
                from core_system.resource_maintenance import release_unused_memory

                app.resource_release = release_unused_memory
                toolbox = app.toolbox_service
                central_repair = None
                if toolbox is not None and hasattr(toolbox, "central_repair"):
                    try:
                        central_repair = toolbox.central_repair
                    except Exception:
                        central_repair = None
                maintenance_report = await app.maintenance_sovereign.start(
                    daily_cleaner=app.daily_global_cleaner_service,
                    hot_update=app.hot_update_service,
                    repair_service=central_repair,
                )
                app._log(
                    {
                        "type": "maintenance_sovereign_startup",
                        "role": maintenance_report.get("role", ""),
                    }
                )
            except Exception as error:
                app._record_startup_failure("maintenance_sovereign", error)

        # 2. Permission Sovereign — read-only permission coordination
        app._mark_startup_phase("permission_sovereign_starting")
        try:
            if app.permission_sovereign is None:
                from .permission_sovereign import PermissionSovereign

                app.permission_sovereign = PermissionSovereign(
                    app,
                    governance=app.governance,
                )
            app._log(
                {
                    "type": "permission_sovereign_startup",
                    "role": app.permission_sovereign.ROLE,
                }
            )
        except Exception as error:
            app._record_startup_failure("permission_sovereign", error)
        app._mark_startup_phase("permission_sovereign_started")

        # 3. Main-system self-maintenance runs before the Decision Sovereign
        #    so that maintenance_ready is already true when resident tools
        #    try to start.  No global lock; the boolean flag is the only gate.
        #    It is independent of the health-maintenance sub-sovereign's
        #    start, so both
        #    run under E155 bounded-independent-parallelism.
        app._mark_startup_phase("sovereign_initializing")
        from core_system.main_system_self_maintenance import MainSystemSelfMaintenance

        app.main_system_self_maintenance = MainSystemSelfMaintenance(
            self.workspace_root,
            authentication=getattr(app.governance, "authentication", None),
        )

        async def _start_self_maintenance() -> None:
            try:
                await app.main_system_self_maintenance.start()
            except Exception as error:
                app._record_startup_failure("main_system_self_maintenance", error)

        await asyncio.gather(_start_maintenance(), _start_self_maintenance())
        step_timings["maintenance-and-self-maintenance_ms"] = int(
            (time.monotonic() - _step_start) * 1000
        )
        _step_start = time.monotonic()
        # CORE-READY condition "maintenance-active" means the maintenance
        # services are activated and running their loops — the deferred
        # startup duty pass reports through _last_report/health monitoring
        # once it completes; it is not an activation gate.
        startup_ok = bool(
            getattr(app.main_system_self_maintenance, "_running", False)
        )
        app.maintenance_ready = startup_ok
        if app.governance is not None:
            app.governance.maintenance_ready = startup_ok

        # 4. Decision Sovereign and its six sub-sovereigns
        try:
            await self.start()
            sovereign = await self._start_sub_sovereigns()
            app._log(
                {
                    "type": "sovereign_startup",
                    "dependency_state": sovereign.get("dependency_state", ""),
                }
            )
        except Exception as error:
            app._record_startup_failure("decision_sovereign", error)
        step_timings["decision-sovereign-and-subsovereigns_ms"] = int(
            (time.monotonic() - _step_start) * 1000
        )
        app._startup_step_timings = step_timings
        app._mark_startup_phase("sovereign_initialized")
        return startup_ok

    async def _start_sub_sovereigns(self) -> dict[str, Any]:
        """Start the Decision Sovereign's own sub-sovereigns.

        The health-maintenance and permission sovereigns are materialized
        by ``start_sovereign_stack`` before this method is called.  This
        method only starts the sub-sovereigns owned by the Decision
        Sovereign: runtime_state, resource_dependency, data_governance,
        channel_contract, language_review, dependency.

        Single-fault isolation: each sub-sovereign is started independently.
        A failure in one does not prevent the rest from starting, and all
        failures are recorded in the report's ``startup_failures`` list.
        """

        dependency_state = self._dependency_state()
        self._startup_failures: list[dict[str, str]] = []

        # All 6 sub-sovereigns are started in parallel because:
        # - None depend on another's start() completing (they reference
        #   app.* attributes already set before this method is called).
        # - Single-fault isolation is already implemented per-sovereign.
        # - This eliminates serial await latency (6 sequential awaits
        #   become 1 concurrent gather).
        memory_maintainer = getattr(self.app, "_idle_memory_maintainer", None)

        async def _start_runtime() -> dict[str, Any]:
            try:
                return await self.runtime_sovereign.start()
            except Exception as error:
                self._startup_failures.append(
                    {"sub_sovereign": "runtime", "error": f"{type(error).__name__}: {error}"}
                )
                return {}

        async def _start_resource() -> dict[str, Any]:
            try:
                return await self.resource_sovereign.start(
                    memory_maintainer=memory_maintainer,
                )
            except Exception as error:
                self._startup_failures.append(
                    {"sub_sovereign": "resource", "error": f"{type(error).__name__}: {error}"}
                )
                return {}

        async def _start_data() -> dict[str, Any]:
            try:
                return await self.data_sovereign.start()
            except Exception as error:
                self._startup_failures.append(
                    {"sub_sovereign": "data", "error": f"{type(error).__name__}: {error}"}
                )
                return {}

        async def _start_integration() -> dict[str, Any]:
            try:
                return await self.integration_sovereign.start()
            except Exception as error:
                self._startup_failures.append(
                    {"sub_sovereign": "integration", "error": f"{type(error).__name__}: {error}"}
                )
                return {}

        async def _start_language_review() -> dict[str, Any]:
            try:
                return await self.language_review_sovereign.start()
            except Exception as error:
                self._startup_failures.append(
                    {"sub_sovereign": "language_review", "error": f"{type(error).__name__}: {error}"}
                )
                return {}

        async def _start_third_party() -> dict[str, Any]:
            try:
                return await self.third_party_sovereign.start()
            except Exception as error:
                self._startup_failures.append(
                    {"sub_sovereign": "third_party", "error": f"{type(error).__name__}: {error}"}
                )
                return {}

        runtime, resource, data, integration, language_review, third_party = (
            await asyncio.gather(
                _start_runtime(),
                _start_resource(),
                _start_data(),
                _start_integration(),
                _start_language_review(),
                _start_third_party(),
            )
        )

        sub_sovereign_roles = [
            result.get("role", "")
            for result in (
                runtime,
                resource,
                data,
                integration,
                language_review,
                third_party,
            )
            if result
        ]

        # E173: the startup report is an activation receipt — role names,
        # failures, and dependency state only.  Full status trees
        # (orchestration_status/live_status) are served on demand by
        # status()/orchestration_status(); composing them inline here would
        # burn the phase budget on reporting, not activation.
        report = {
            "ok": len(self._startup_failures) == 0,
            "sovereign": "decision-sovereign",
            "dependency_state": dependency_state,
            "started_at": _iso_now(),
            "execution_delegation": "governed-executor-only",
            "sub_sovereigns": sub_sovereign_roles,
            "startup_failures": list(self._startup_failures),
            "peer_systems": {
                "learning": getattr(
                    self.learning_system_sovereign, "_started", False
                ),
                "programming": getattr(
                    self.system_programming_sovereign, "_started", False
                ),
            },
            "health_owner": "health-maintenance-test-sub-sovereign",
            "sources": [
                {"kind": "env", "name": "GPTBRIDGE_STARTUP_STATE"},
                {
                    "kind": "report",
                    "path": str(self.launcher_report_path),
                },
            ],
        }
        self._save_state(report)
        return report

    async def stop(self) -> None:
        """Stop only the sub-sovereigns owned by the Decision Sovereign.

        The health-maintenance sub-sovereign and Permission Sovereign are
        stopped by the app.
        """
        for sovereign in (
            self.third_party_sovereign,
            self.language_review_sovereign,
            self.integration_sovereign,
            self.data_sovereign,
            self.resource_sovereign,
            self.runtime_sovereign,
        ):
            try:
                await sovereign.stop()
            except Exception:
                pass
        await self.system_programming_sovereign.stop()
        await self.learning_system_sovereign.stop()
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
                self.runtime_sovereign.live_status(),
                self.resource_sovereign.live_status(),
                self.data_sovereign.live_status(),
                self.integration_sovereign.live_status(),
                self.language_review_sovereign.live_status(),
                self.third_party_sovereign.live_status(),
            ],
            "peer_systems": {
                "learning": self.learning_system_sovereign.status(),
                "programming": self.system_programming_sovereign.status(),
            },
            "health_owner": "health-maintenance-test-sub-sovereign",
            "governance_rules": self.governance_rule_coordination.coordination_status(),
            "runtime": self.runtime_sovereign.live_status(),
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
            "resource": self.resource_sovereign.live_status(),
            "data": self.data_sovereign.live_status(),
            "integration": self.integration_sovereign.live_status(),
            "language_review": self.language_review_sovereign.live_status(),
            "third_party": self.third_party_sovereign.live_status(),
        }

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["sub_sovereign_registry"] = {
            name: sov.live_status() if hasattr(sov, "live_status") else {"role": name}
            for name, sov in self._sub_sovereigns.items()
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
                self.runtime_sovereign.orchestration_status(),
                self.resource_sovereign.orchestration_status(),
                self.data_sovereign.orchestration_status(),
                self.integration_sovereign.orchestration_status(),
                self.language_review_sovereign.orchestration_status(),
                self.third_party_sovereign.orchestration_status(),
            ],
            "peer_systems": {
                "learning": self.learning_system_sovereign.status(),
                "programming": self.system_programming_sovereign.status(),
            },
            "health_owner": "health-maintenance-test-sub-sovereign",
            "governance_rules": self.governance_rule_coordination.orchestration_status(),
            "runtime": self.runtime_sovereign.orchestration_status(),
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
            "resource": self.resource_sovereign.orchestration_status(),
            "data": self.data_sovereign.orchestration_status(),
            "integration": self.integration_sovereign.orchestration_status(),
            "language_review": self.language_review_sovereign.orchestration_status(),
            "third_party": self.third_party_sovereign.orchestration_status(),
            "subsystems": [
                self.governance_rule_coordination.orchestration_status(),
                self.runtime_sovereign.orchestration_status(),
                self.resource_sovereign.orchestration_status(),
                self.data_sovereign.orchestration_status(),
                self.integration_sovereign.orchestration_status(),
                self.language_review_sovereign.orchestration_status(),
                self.third_party_sovereign.orchestration_status(),
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
