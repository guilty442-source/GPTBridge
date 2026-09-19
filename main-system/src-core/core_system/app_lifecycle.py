"""Application Lifecycle Management.

Core application lifecycle including initialization, startup phase tracking,
sub-sovereign registry, and governance rules management.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Add src-core to sys.path so absolute imports work when this is not run as a module
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "shared-layer" / "src"),
)

from governance_rule.governance_policy import (
    DEFAULT_ACTIVE_GOVERNANCE_RULES,
    GOVERNANCE_RULE_CATALOG,
)
from core_system.runtime_bootstrap import RuntimeBootstrap
from core_system.governance_runtime import MainSystemGovernance
from core_system.hot_update_service import HotUpdateService
from core_system.daily_global_cleaner_service import DailyGlobalCleanerService
from core_system.versioning import application_version
from tasks.queue import TaskQueue
from tasks.toolbox_service import ToolboxService
from tasks.runtime_status_service import RuntimeStatusService

from core_system.update_manager import UpdateManager
from core_system.system_automation_coordinator import SystemAutomationCoordinator
from governance.sovereigns import (
    DecisionSovereign,
    PermissionSovereign,
    SystemRuntimeSovereign,
    AutomationSovereign,
    XingchengSovereign,
)

from main_shutdown import GPTBridgeAppShutdownMixin
from core_system.maintenance_controller_integration import create_maintenance_controller_integration
from core_system.rag_runtime_integration import create_rag_runtime_integration
from core_system.cag_integration import create_cag_integration


class AppLifecycleMixin:
    """Application lifecycle management mixin for GPTBridgeApp."""

    # Full catalog of supported governance rules for UI selection menus.
    AVAILABLE_GOVERNANCE_RULES = list(GOVERNANCE_RULE_CATALOG)

    def __init__(self) -> None:
        project_root_override = os.environ.get("GPTBRIDGE_PROJECT_ROOT")
        self.project_root = (
            Path(os.path.abspath(project_root_override))
            if project_root_override
            else Path(__file__).resolve().parents[2]
        )
        self.version = application_version(self.project_root)
        self.maintenance_ready = False
        self.toolbox_service: ToolboxService | None = None
        self.runtime_status_service: RuntimeStatusService | None = None
        self.command_router: Any | None = None
        self.core_logger: None = None
        self.governance: MainSystemGovernance | None = None
        self.task_queue: TaskQueue | None = None
        self.runtime_bootstrap = RuntimeBootstrap(self)
        self.hot_update_service = HotUpdateService(self)
        self.daily_global_cleaner_service = DailyGlobalCleanerService(self)
        self.update_manager: UpdateManager | None = None

        # Maintenance controller integration (Database Auto Maintenance v1)
        self.maintenance_controller_integration = create_maintenance_controller_integration(self)
        self.rag_ready = False
        self.cag_ready = False
        self.rag_runtime = create_rag_runtime_integration(self)
        self.cag_integration = create_cag_integration(self)

        # New governance architecture sovereigns (A63/A64/A12/A128)
        # Decision layer sovereigns
        self.decision_sovereign = DecisionSovereign(self)
        self.permission_sovereign = PermissionSovereign(self)
        self.system_runtime_sovereign = SystemRuntimeSovereign(self)
        self.automation_sovereign = AutomationSovereign(self)
        self.xingcheng_sovereign = XingchengSovereign(self)

        # System-wide automation coordinator (A63/A64 decision-layer).
        # Unifies all sovereign automation loops into a single
        # coordination surface with cross-sovereign health monitoring.
        self.system_automation_coordinator = SystemAutomationCoordinator(self)

        # Sub-sovereigns (initialized on demand, parent set via set_parent)
        self._sub_sovereigns: dict[str, Any] = {}
        self._sub_sovereign_classes: dict[str, str] = {
            "system-sub-sovereign": "SystemSubSovereign",
            "startup-sub-sovereign": "StartupSubSovereign",
            "directory-sub-sovereign": "DirectorySubSovereign",
            "identity-group-sub-sovereign": "IdentityGroupSubSovereign",
            "resource-dependency-sync-sub-sovereign": "ResourceDependencySyncSubSovereign",
            "channel-contract-sync-sub-sovereign": "ChannelContractSyncSubSovereign",
            "policy-architecture-sub-sovereign": "PolicyArchitectureSubSovereign",
            "health-maintenance-test-sub-sovereign": "HealthMaintenanceTestSubSovereign",
            "data-governance-sub-sovereign": "DataGovernanceSubSovereign",
            "priority-capability-sub-sovereign": "PriorityCapabilitySubSovereign",
            "change-acceptance-sub-sovereign": "ChangeAcceptanceSubSovereign",
            "dependency-sync-sub-sovereign": "DependencySyncSubSovereign",
            "release-update-sync-sub-sovereign": "ReleaseUpdateSyncSubSovereign",
            "runtime-state-sync-sub-sovereign": "RuntimeStateSyncSubSovereign",
            "repair-backup-sync-sub-sovereign": "RepairBackupSyncSubSovereign",
            "cleanup-retention-sync-sub-sovereign": "CleanupRetentionSyncSubSovereign",
            "learning-evidence-sync-sub-sovereign": (
                "governance.sovereigns.xingcheng.learning_sub_sovereign:"
                "LearningEvidenceSyncSubSovereign"
            ),
            "automatic-log-sync-sub-sovereign": "AutomaticLogSyncSubSovereign",
        }

        self.hot_reload_watcher: Any | None = None
        self.authority_reanchor_service: Any | None = None
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._command_task_meta: dict[asyncio.Task[Any], dict[str, Any]] = {}
        # A67 connection counters.  ``_active_ws_connections`` tracks any open
        # WebSocket socket (for the connection watchdog).  The independent
        # ``_authenticated_ipc_connections`` counter is incremented ONLY for
        # sockets that passed ``_websocket_request_authorized`` in the
        # handshake — it is the verified channel the readiness gate consults
        # for condition 4 (authenticated-ipc-connected), not an inference
        # from the session token.
        self._active_ws_connections: int = 0
        self._authenticated_ipc_connections: int = 0

        self.startup_phase = "created"
        self.startup_phase_active_since = time.monotonic()
        self.startup_phase_history: list[dict[str, Any]] = []
        self._shutdown_started = False
        self._shutdown_complete = asyncio.Event()
        self.default_tool_startup: dict[str, dict[str, Any]] = {}
        self.startup_failures: list[dict[str, Any]] = []
        self.startup_dead = False

    def _mark_startup_phase(self, phase: str) -> None:
        now = time.monotonic()
        previous = getattr(self, "startup_phase", None)
        duration_ms = None
        if previous is not None and previous != phase:
            duration_ms = int((now - self.startup_phase_active_since) * 1000)
            self.startup_phase_history.append(
                {
                    "phase": previous,
                    "duration_ms": duration_ms,
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        self.startup_phase = phase
        self.startup_phase_active_since = now
        try:
            self._log(
                {
                    "type": "startup_phase",
                    "phase": phase,
                    "duration_since_last_ms": duration_ms,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
        except Exception:
            pass

    def get_startup_status(self) -> dict[str, Any]:
        now = time.monotonic()
        active_duration_ms = int((now - self.startup_phase_active_since) * 1000)
        return {
            "phase": getattr(self, "startup_phase", "unknown"),
            "phase_duration_ms": active_duration_ms,
            "phase_history": list(self.startup_phase_history),
            "maintenance_ready": self.maintenance_ready,
            "default_tools": dict(self.default_tool_startup),
            "startup_failures": list(self.startup_failures),
            "startup_dead": self.startup_dead,
            "daily_global_cleaner": self.daily_global_cleaner_service.status(),
            # New governance architecture status
            "decision_sovereign": self.decision_sovereign.live_status(),
            "permission_sovereign": self.permission_sovereign.coordination_status(),
            "system_runtime_sovereign": self.system_runtime_sovereign.live_status(),
            "automation_sovereign": self.automation_sovereign.live_status(),
            "xingcheng_sovereign": self.xingcheng_sovereign.live_status(),
            "system_automation": self.system_automation_coordinator.system_status(),
            "sub_sovereigns": self._collect_sub_sovereign_status(),
        }

    def _collect_sub_sovereign_status(self) -> dict[str, Any]:
        """Aggregate live status from every materialized codex child.

        The executor registers children into each codex parent's own
        ``_sub_sovereigns`` registry (A334) — the app-level dict is a
        legacy surface that is usually empty, so enumerate the parents'
        registries via the hierarchy registry instead.
        """
        collected: dict[str, Any] = dict(
            getattr(self, "_sub_sovereigns", {}) or {}
        )
        try:
            from governance.registries import children_of, resolve_sovereign

            for parent_id in (
                "decision-sovereign",
                "permission-sovereign",
                "system-runtime-sovereign",
                "automation-sovereign",
                # A485: 星澄's learning sub-sovereign surfaces here too.
                "星澄",
            ):
                parent = resolve_sovereign(self, parent_id)
                registry = getattr(parent, "_sub_sovereigns", None)
                if not registry:
                    continue
                for child_id in children_of(parent_id):
                    child = registry.get(child_id)
                    if child is not None:
                        collected.setdefault(child_id, child)
        except Exception:
            pass
        out: dict[str, Any] = {}
        for name, sov in collected.items():
            live = getattr(sov, "live_status", None)
            try:
                out[name] = live() if callable(live) else {"started": False}
            except Exception:
                out[name] = {"error": "live_status-failed"}
        return out

    def get_sub_sovereign(self, name: str) -> Any | None:
        """Lazy-load a sub-sovereign by name (e.g., 'startup-sub-sovereign')."""
        if name in self._sub_sovereigns:
            return self._sub_sovereigns[name]
        class_ref = self._sub_sovereign_classes.get(name)
        if not class_ref:
            return None
        try:
            if ":" in class_ref:
                # Dotted ``module:Class`` reference (A485: the learning
                # sub-sovereign lives in the 星澄 owner package).
                module_name, class_name = class_ref.split(":", 1)
                module = __import__(module_name, fromlist=[class_name])
                cls = getattr(module, class_name)
            else:
                from governance.sub_sovereigns import __all__ as _all
                if class_ref not in _all:
                    return None
                module = __import__("governance.sub_sovereigns", fromlist=[class_ref])
                cls = getattr(module, class_ref)
            instance = cls(self)
            self._sub_sovereigns[name] = instance
            return instance
        except Exception:
            return None

    def _load_governance_rules(self) -> list[str]:
        """Return the versioned, immutable main-system governance catalog."""

        return list(DEFAULT_ACTIVE_GOVERNANCE_RULES)

    def _normalize_global_governance_rules(self, rules: Any) -> list[str]:
        catalog = [str(item).strip() for item in self.AVAILABLE_GOVERNANCE_RULES]
        incoming = rules if isinstance(rules, list) else []
        normalized = [str(item).strip() for item in incoming if str(item).strip()]
        return list(dict.fromkeys([*catalog, *normalized]))

    def _save_governance_rules(self) -> None:
        raise PermissionError("Governance rules are immutable at runtime")

    def _log(self, data: dict[str, Any]) -> None:
        print(json.dumps(data, ensure_ascii=False), flush=True)

    def _record_startup_failure(self, stage: str, error: BaseException) -> None:
        """Single-fault isolation: record a stage failure and keep starting."""

        failure = {
            "stage": stage,
            "error": f"{type(error).__name__}: {error}",
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self.startup_failures.append(failure)
        self._log({"type": "startup_failure", **failure})


__all__ = ["AppLifecycleMixin"]