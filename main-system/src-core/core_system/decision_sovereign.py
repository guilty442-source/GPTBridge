"""System Sovereign — top-level startup dispatch after dependency checks.

The launcher (start.ps1) performs environment loading, runtime checks,
PostgreSQL/Qdrant/Ollama probing, and a governance audit BEFORE the Electron
and Python backend are launched.  That validated dependency state is handed to
the backend through two channels:

  * GPTBRIDGE_STARTUP_STATE            -- the READY/DEGRADED/RECOVERY string
  * <main-system>/launcher/state/orchestrator-report.json -- full service report

Per A128/A130 (supersedes A63/A64), the mother process (GPTBridgeApp) must
not directly materialize or start sovereigns.  Instead, it delegates the
sovereign stack startup to this service via
``DecisionSovereignService.start_sovereign_stack``.  This service starts its
own in-process sub-sovereigns and coordinates with the maintenance and
permission sovereigns already started by the app:

  1. 維護主宰 (Maintenance Sovereign)  — system-health monitoring/preservation
  2. 權限主宰 (Permission Sovereign)   — permission management (read-only surface)
  3. 系統主宰 (System Sovereign)       — this service; starts its own sub-sovereigns

The decision-sovereign (this service) also owns the repair DECISION
chain per A152/A154/E127/E128: the maintenance sovereign classifies health
signals (health-only scope) and hands them to this sovereign, which makes
the repair decision, validates permissions, and routes to the
system-programming (code change) or system-runtime (runtime action)
sub-sovereign for governed execution.

The System Sovereign starts its own in-process sub-sovereigns:
  * runtime-sub-sovereign       -- keeps the platform running and serving
  * resource-sub-sovereign      -- owns all resource-body concerns
  * data-sub-sovereign          -- owns all data-body concerns
  * integration-sub-sovereign   -- owns cross-sovereign structural interfaces
  * language-review-sub-sovereign -- programming-language conformance
  * third-party-sub-sovereign   -- third-party software management

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

from .data_sub_sovereign import DataSubSovereign
from .governance_rule_coordination import GovernanceRuleCoordination
from .integration_sub_sovereign import IntegrationSubSovereign
from .language_review_sub_sovereign import LanguageReviewSubSovereign
from .learning_system_sovereign import LearningSystemSovereign
from .main_system_self_maintenance import MainSystemSelfMaintenance
from .permission_sovereign import PermissionSovereign
from .resource_sub_sovereign import ResourceSubSovereign
from .repair_decision_chain import RepairDecisionChain
from .runtime_sub_sovereign import RuntimeSubSovereign
from .sovereign_utils import _iso_now
from .third_party_sub_sovereign import ThirdPartySubSovereign
from .system_programming_sovereign import SystemProgrammingSovereign



_SYSTEM_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "decision"),
    None,
)
if _SYSTEM_SOVEREIGN is None:
    raise RuntimeError("decision sovereign not found in Governance Codex")

DECISION_SOVEREIGN_RESPONSIBILITIES = _SYSTEM_SOVEREIGN.duties


class DecisionSovereignService:
    """Sovereign-stack startup dispatcher (A64) and platform sub-sovereign owner.

    Responsibilities at startup:
      - Receive the dispatch from GPTBridgeApp to materialize the entire
        sovereign stack in order (maintenance, permission, self-maintenance,
        then the System Sovereign and its sub-sovereigns)
      - Consume the validated dependency state (env var + orchestrator report)
      - Record the sovereign startup phase into the platform startup status
      - Start its own sub-sovereigns: Runtime, Resource, Data, Integration,
        Language Review, Third-Party
      - Coordinate (read-only) the Maintenance Sovereign and Permission Sovereign
        after this service has materialized them
      - Delegate all execution to governed executors (never in this process)
    """

    ROLE = _SYSTEM_SOVEREIGN.id

    def __init__(self, app: Any) -> None:
        self.app = app
        workspace_root = Path(getattr(app, "project_root", Path.cwd()))
        self.workspace_root = workspace_root.resolve()
        self.runtime_state_path = (
            self.workspace_root
            / "main-system"
            / "runtime"
            / "state"
            / "system-sovereign.json"
        )
        self.launcher_report_path = (
            self.workspace_root
            / "main-system"
            / "launcher"
            / "state"
            / "orchestrator-report.json"
        )
        self.platform_id = "main-system"
        self.module_id = "system-sovereign"
        # Sub-sovereigns owned and started by the System Sovereign.
        self.runtime_sovereign = RuntimeSubSovereign(app)
        self.resource_sovereign = ResourceSubSovereign(app)
        self.data_sovereign = DataSubSovereign(app)
        self.integration_sovereign = IntegrationSubSovereign(app)
        self.language_review_sovereign = LanguageReviewSubSovereign(app)
        self.third_party_sovereign = ThirdPartySubSovereign(app)
        self.learning_system_sovereign = LearningSystemSovereign(app)
        self.system_programming_sovereign = SystemProgrammingSovereign(app)
        self.governance_rule_coordination = GovernanceRuleCoordination(app)
        # A152/A154/E127/E128: the decision-sovereign owns the repair
        # decision chain.  The maintenance sovereign classifies health
        # signals (health-only) and delegates the decision here.
        self._repair_decision_chain = RepairDecisionChain(app)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start_sovereign_stack(self) -> bool:
        """Governed-executor startup of the entire sovereign stack.

        Per A63 (sovereigns are decision-only) and A64 (sub-sovereigns
        control/dispatch under parent authority), the mother process
        (GPTBridgeApp) must not directly materialize sovereigns.  Instead,
        it dispatches to this service, which materializes the top-level
        sovereigns in order and then starts the System Sovereign's own
        sub-sovereigns.  All work remains delegated to governed executors.

        Order (E155 bounded-independent-parallelism within each step):
          1. Peer decision sovereigns + daily cleaner (learning,
             programming, cleaner — independent, started in parallel)
          2. Permission Sovereign (read-only coordination surface)
          3. Maintenance Sovereign + main-system self-maintenance
             (independent — started in parallel)
          4. System Sovereign and its six sub-sovereigns

        Returns the maintenance_ready flag for the final readiness log.
        """

        app = self.app
        # Step timings feed the startup executor's phase evidence so a
        # deadline breach reports the exact bottleneck (E173).
        step_timings: dict[str, int] = {}
        _step_start = time.monotonic()

        # Learning and programming are peer decision sovereigns. They start
        # before maintenance so every subsequent failure and repair can be
        # learned, and every code change has one governed dispatch owner.
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

        # 1. Maintenance Sovereign — periodic maintenance, health, repair
        app._mark_startup_phase("maintenance_sovereign_starting")

        async def _start_maintenance() -> None:
            try:
                from .resource_maintenance import release_unused_memory

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

        # 3. Main-system self-maintenance runs before the System Sovereign
        #    so that maintenance_ready is already true when resident tools
        #    try to start.  No global lock; the boolean flag is the only gate.
        #    It is independent of the maintenance sovereign's start, so both
        #    run under E155 bounded-independent-parallelism.
        app._mark_startup_phase("sovereign_initializing")
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

        # 4. System Sovereign and its six sub-sovereigns
        try:
            sovereign = await self.start()
            app._log(
                {
                    "type": "sovereign_startup",
                    "dependency_state": sovereign.get("dependency_state", ""),
                }
            )
        except Exception as error:
            app._record_startup_failure("decision_sovereign", error)
        step_timings["system-sovereign-and-subsovereigns_ms"] = int(
            (time.monotonic() - _step_start) * 1000
        )
        app._startup_step_timings = step_timings
        app._mark_startup_phase("sovereign_initialized")
        return startup_ok

    async def start(self) -> dict[str, Any]:
        """Start the System Sovereign's own sub-sovereigns.

        The top-level Maintenance and Permission sovereigns are materialized
        by ``start_sovereign_stack`` before this method is called.  This
        method only starts the sub-sovereigns owned by the System Sovereign:
        runtime, resource, data, integration, language_review, third_party.

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
            "sovereign": "system-sovereign",
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
            "health_owner": "maintenance-sovereign",
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
        """Stop only the sub-sovereigns owned by the System Sovereign.

        Maintenance Sovereign and Permission Sovereign are stopped by the app.
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

    # ------------------------------------------------------------------
    # Repair decision (A152/A154/E127/E128)
    # ------------------------------------------------------------------

    def decide_and_route_repair(self, classified_signal: dict[str, Any]) -> dict[str, Any]:
        """A152 repair-decision entry point for the decision-sovereign.

        Per A152 (supersedes A67): ``REPAIR-DECISION:decision-sovereign``
        and ``FORBID:maintenance-owning-non-health-decisions``.  The
        maintenance sovereign classifies the health signal (health-only
        scope, A154) and delegates the repair DECISION here.  This method
        routes the classified signal through permission validation and
        governed execution (E128):
        ``decision > permission > runtime-or-programming > executor
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
            "sovereign": "system-sovereign",
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
            "health_owner": "maintenance-sovereign",
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

    def orchestration_status(self) -> dict[str, Any]:
        """Snap the governing orchestrator's unified subsystem health.

        The Xingcheng auxiliary system lives in the local-model governed
        executor process, keeping the heavy AI/model runtime isolated from the
        mother process (consistent with execution_delegation =
        governed-executor-only).  The sovereign coordinates it and surfaces the
        read-only governance rule authority, it does not import the heavy stack
        in-process and never mutates governance.  System health determination is
        owned by the maintenance sovereign, not by this top sovereign.
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
            "health_owner": "maintenance-sovereign",
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


__all__ = ["DECISION_SOVEREIGN_RESPONSIBILITIES", "DecisionSovereignService"]
