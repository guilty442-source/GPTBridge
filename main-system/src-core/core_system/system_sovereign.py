"""System Sovereign — top-level startup entry after dependency checks.

The launcher (start.ps1) performs environment loading, runtime checks,
PostgreSQL/Qdrant/Ollama probing, and a governance audit BEFORE the Electron
and Python backend are launched.  That validated dependency state is handed to
the backend through two channels:

  * GPTBRIDGE_STARTUP_STATE            -- the READY/DEGRADED/RECOVERY string
  * <main-system>/launcher/state/orchestrator-report.json -- full service report

This service is the "System Sovereign": it is created after those dependency
checks have passed, it owns the startup lifecycle of the platform, and it
coordinates — but does not directly execute — the governing subsystems (the
Xingcheng core orchestrator and its SQL/RAG/Git managers live in the local-model
governed-executor process, kept isolated from this mother process).

The System Sovereign is split into two in-process sub-sovereigns:
  * runtime-sovereign    -- keeps the platform running and serving
  * maintenance-sovereign-- owns periodic/background maintenance

Both are LOCAL CODE (same process as GPTBridgeApp) and coordinate existing
in-process services; they never run heavy work in this mother process.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .data_sovereign import DataSovereign
from .governance_rule_coordination import GovernanceRuleCoordination
from .integration_sovereign import IntegrationSovereign
from .maintenance_sovereign import MaintenanceSovereign
from .permission_sovereign import PermissionSovereign
from .resource_sovereign import ResourceSovereign
from .runtime_sovereign import RuntimeSovereign
from .xingcheng_coordination import XingchengCoordination


class SystemSovereignService:
    """Created after launcher dependency checks; owns the platform startup.

    Responsibilities at startup:
      - Consume the validated dependency state (env var + orchestrator report)
      - Record the sovereign startup phase into the platform startup status
      - Own the Runtime Sovereign and Maintenance Sovereign roles
      - Coordinate the Xingcheng auxiliary system (intelligent-management)
      - Coordinate the read-only Permission Sovereign (permission directory)
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
        self.runtime_sovereign = RuntimeSovereign(app)
        self.maintenance_sovereign = MaintenanceSovereign(app)
        self.resource_sovereign = ResourceSovereign(app)
        self.data_sovereign = DataSovereign(app)
        self.integration_sovereign = IntegrationSovereign(app)
        self.xingcheng_coordination = XingchengCoordination(app)
        self.governance_rule_coordination = GovernanceRuleCoordination(app)
        self.permission_sovereign = PermissionSovereign(app)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Start the System Sovereign and its in-process sub-sovereigns."""

        dependency_state = self._dependency_state()

        # Runtime Sovereign coordinates the mother process's liveness services.
        memory_maintainer = getattr(self.app, "_idle_memory_maintainer", None)
        runtime = await self.runtime_sovereign.start(
            memory_maintainer=memory_maintainer,
        )

        # Maintenance Sovereign coordinates periodic/background maintenance.
        from core_system.resource_maintenance import release_unused_memory

        toolbox = getattr(self.app, "toolbox_service", None)
        central_repair = None
        if toolbox is not None and hasattr(toolbox, "central_repair"):
            try:
                central_repair = toolbox.central_repair()
            except Exception:
                central_repair = None

        maintenance = await self.maintenance_sovereign.start(
            daily_cleaner=getattr(self.app, "daily_global_cleaner_service", None),
            resource_release=release_unused_memory,
            hot_update=getattr(self.app, "hot_update_service", None),
            repair_service=central_repair,
        )

        # Resource Sovereign coordinates all resource-body concerns.
        resource = await self.resource_sovereign.start(
            memory_maintainer=memory_maintainer,
        )

        # Data Sovereign coordinates all data-body concerns.
        data = await self.data_sovereign.start()

        # Integration Sovereign coordinates all cross-sovereign-module structural interface concerns.
        integration = await self.integration_sovereign.start()

        report = {
            "ok": True,
            "sovereign": "system-sovereign",
            "dependency_state": dependency_state,
            "started_at": self._iso_now(),
            "execution_delegation": "governed-executor-only",
            "sub_sovereigns": [runtime["role"], maintenance["role"], resource["role"], data["role"], integration["role"]],
            "peer_systems": {
                "xingcheng": self.xingcheng_coordination.orchestration_status(),
            },
            "health_owner": "maintenance-sovereign",
            "governance_rules": self.governance_rule_coordination.orchestration_status(),
            "runtime": self.runtime_sovereign.orchestration_status(),
            "permission": self.permission_sovereign.orchestration_status(),
            "resource": self.resource_sovereign.orchestration_status(),
            "data": self.data_sovereign.orchestration_status(),
            "integration": self.integration_sovereign.orchestration_status(),
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
        await self.integration_sovereign.stop()
        await self.data_sovereign.stop()
        await self.resource_sovereign.stop()
        await self.maintenance_sovereign.stop()
        await self.runtime_sovereign.stop()
        self._save_state({"stopped_at": self._iso_now()})

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        state = self._load_state()
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
                self.maintenance_sovereign.live_status(),
                self.resource_sovereign.live_status(),
                self.data_sovereign.live_status(),
                self.integration_sovereign.live_status(),
            ],
            "peer_systems": {
                "xingcheng": self.xingcheng_coordination.coordination_status(),
            },
            "health_owner": "maintenance-sovereign",
            "governance_rules": self.governance_rule_coordination.coordination_status(),
            "runtime": self.runtime_sovereign.live_status(),
            "permission": self.permission_sovereign.coordination_status(),
            "resource": self.resource_sovereign.live_status(),
            "data": self.data_sovereign.live_status(),
            "integration": self.integration_sovereign.live_status(),
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

        return {
            "state": "delegated",
            "owner": self.module_id,
            "sub_sovereigns": [
                self.runtime_sovereign.orchestration_status(),
                self.maintenance_sovereign.orchestration_status(),
                self.resource_sovereign.orchestration_status(),
                self.data_sovereign.orchestration_status(),
                self.integration_sovereign.orchestration_status(),
            ],
            "peer_systems": {
                "xingcheng": self.xingcheng_coordination.orchestration_status(),
            },
            "health_owner": "maintenance-sovereign",
            "governance_rules": self.governance_rule_coordination.orchestration_status(),
            "runtime": self.runtime_sovereign.orchestration_status(),
            "permission": self.permission_sovereign.orchestration_status(),
            "resource": self.resource_sovereign.orchestration_status(),
            "data": self.data_sovereign.orchestration_status(),
            "integration": self.integration_sovereign.orchestration_status(),
            "subsystems": [
                self.governance_rule_coordination.orchestration_status(),
                self.runtime_sovereign.orchestration_status(),
                self.permission_sovereign.orchestration_status(),
                self.resource_sovereign.orchestration_status(),
                self.data_sovereign.orchestration_status(),
                self.integration_sovereign.orchestration_status(),
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
