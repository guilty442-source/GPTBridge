"""System Sovereign — top-level startup entry after dependency checks.

The launcher (start.ps1) performs environment loading, runtime checks,
PostgreSQL/Qdrant/Ollama probing, and a governance audit BEFORE the Electron
and Python backend are launched.  That validated dependency state is handed to
the backend through two channels:

  * GPTBRIDGE_STARTUP_STATE            -- the READY/DEGRADED/RECOVERY string
  * <main-system>/launcher/state/orchestrator-report.json -- full service report

The startup core (boot_core / GPTBridgeApp.initialize) starts three top-level
sovereigns in order:

  1. 維護主宰 (Maintenance Sovereign)  — periodic maintenance, health, repair
  2. 權限主宰 (Permission Sovereign)   — permission management (read-only surface)
  3. 系統主宰 (System Sovereign)       — this service; starts its own sub-sovereigns

The System Sovereign starts its own in-process sub-sovereigns:
  * runtime-sub-sovereign       -- keeps the platform running and serving
  * resource-sub-sovereign      -- owns all resource-body concerns
  * data-sub-sovereign          -- owns all data-body concerns
  * integration-sub-sovereign   -- owns cross-sovereign structural interfaces
  * language-review-sub-sovereign -- programming-language conformance
  * third-party-sub-sovereign   -- third-party software management

The Maintenance Sovereign and Permission Sovereign are started by the app
BEFORE this service; this service coordinates them (for status reporting) but
does not own their startup or shutdown.

All are LOCAL CODE (same process as GPTBridgeApp) and coordinate existing
in-process services; they never run heavy work in this mother process.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data_sub_sovereign import DataSubSovereign
from .governance_rule_coordination import GovernanceRuleCoordination
from .integration_sub_sovereign import IntegrationSubSovereign
from .language_review_sub_sovereign import LanguageReviewSubSovereign
from .resource_sub_sovereign import ResourceSubSovereign
from .runtime_sub_sovereign import RuntimeSubSovereign
from .third_party_sub_sovereign import ThirdPartySubSovereign
from .xingcheng_coordination import XingchengCoordination


class SystemSovereignService:
    """Created after maintenance and permission sovereigns; owns the platform
    sub-sovereign startup.

    Responsibilities at startup:
      - Consume the validated dependency state (env var + orchestrator report)
      - Record the sovereign startup phase into the platform startup status
      - Start its own sub-sovereigns: Runtime, Resource, Data, Integration,
        Language Review, Third-Party
      - Coordinate (read-only) the Maintenance Sovereign and Permission Sovereign
        already started by the app
      - Coordinate the Xingcheng auxiliary system (intelligent-management)
      - Delegate all execution to governed executors (never in this process)
    """

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
        self.platform_id = "local-model-platform"
        self.module_id = "xingcheng"
        # Sub-sovereigns owned and started by the System Sovereign.
        self.runtime_sovereign = RuntimeSubSovereign(app)
        self.resource_sovereign = ResourceSubSovereign(app)
        self.data_sovereign = DataSubSovereign(app)
        self.integration_sovereign = IntegrationSubSovereign(app)
        self.language_review_sovereign = LanguageReviewSubSovereign(app)
        self.third_party_sovereign = ThirdPartySubSovereign(app)
        self.xingcheng_coordination = XingchengCoordination(app)
        self.governance_rule_coordination = GovernanceRuleCoordination(app)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Start the System Sovereign's own sub-sovereigns.

        Maintenance Sovereign and Permission Sovereign are started by the app
        BEFORE this method is called; this method only starts the sub-sovereigns
        owned by the System Sovereign: runtime, resource, data, integration,
        language_review, third_party.

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
                return await self.runtime_sovereign.start(
                    memory_maintainer=memory_maintainer,
                )
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

        # Coordinate (read-only) the maintenance and permission sovereigns
        # already started by the app.
        maintenance_sovereign = getattr(self.app, "maintenance_sovereign", None)
        permission_sovereign = getattr(self.app, "permission_sovereign", None)

        report = {
            "ok": len(self._startup_failures) == 0,
            "sovereign": "system-sovereign",
            "dependency_state": dependency_state,
            "started_at": self._iso_now(),
            "execution_delegation": "governed-executor-only",
            "sub_sovereigns": sub_sovereign_roles,
            "startup_failures": list(self._startup_failures),
            "peer_systems": {
                "xingcheng": self.xingcheng_coordination.orchestration_status(),
            },
            "health_owner": "maintenance-sovereign",
            "governance_rules": self.governance_rule_coordination.orchestration_status(),
            "runtime": self.runtime_sovereign.orchestration_status(),
            "maintenance": (
                maintenance_sovereign.live_status()
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
        self._save_state({"stopped_at": self._iso_now()})

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
                "xingcheng": self.xingcheng_coordination.coordination_status(),
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
                "xingcheng": self.xingcheng_coordination.orchestration_status(),
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

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()

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
