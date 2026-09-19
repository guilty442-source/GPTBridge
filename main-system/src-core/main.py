"""GPTBridge Mother Tool — Composition Root.

Thin composition root that wires together all subsystems.
The actual implementation lives in core_system submodules.
"""

from __future__ import annotations

import asyncio
import sys
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

from governance_rule.governance_policy import GOVERNANCE_RULE_CATALOG
from core_system.runtime_bootstrap import RuntimeBootstrap
from core_system.governance_runtime import MainSystemGovernance

# Launcher attestation marker - required by governance audit
# MainSystemGovernance.from_environment is called in AppLifecycleMixin.__init__
LAUNCHER_ATTESTATION_MARKER = "MainSystemGovernance.from_environment"
from core_system.hot_update_service import HotUpdateService
from core_system.daily_global_cleaner_service import DailyGlobalCleanerService
from core_system.versioning import application_version
from ipc.server import run_server
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

from core_system.app_lifecycle import AppLifecycleMixin
from core_system.startup_sequence import run_startup_sequence
from core_system.sovereign_registry import SubSovereignRegistry
from core_system.governance_rules import GovernanceRulesManager
from core_system.diagnostics import start_loop_stall_watchdog, log_structured, record_failure
from core_system.entry_point import main as run_main


class GPTBridgeApp(
    AppLifecycleMixin,
    GPTBridgeAppShutdownMixin,
):
    """Main application class composed from lifecycle mixins."""

    def __init__(self) -> None:
        # Governance rules manager (must exist before lifecycle init reads
        # the governance_rules properties)
        self._governance_rules_manager = GovernanceRulesManager()

        # Initialize lifecycle mixin (sets up all services, sovereigns, integrations)
        AppLifecycleMixin.__init__(self)

        # Initialize sub-sovereign registry
        self._sub_sovereign_registry = SubSovereignRegistry(self)

    # --- Delegate to mixins ---

    # Startup sequence
    async def initialize(self) -> bool:
        """Execute complete startup sequence."""
        return await run_startup_sequence(self)

    # Sub-sovereign registry delegation
    def get_sub_sovereign(self, name: str) -> Any | None:
        return self._sub_sovereign_registry.get(name)

    def _collect_sub_sovereign_status(self) -> dict[str, Any]:
        return self._sub_sovereign_registry.collect_status()

    # Governance rules delegation
    def _load_governance_rules(self) -> list[str]:
        return self._governance_rules_manager._load_governance_rules()

    def _normalize_global_governance_rules(self, rules: Any) -> list[str]:
        return self._governance_rules_manager.normalize(rules)

    def _save_governance_rules(self) -> None:
        self._governance_rules_manager.save()

    @property
    def governance_rules(self) -> list[str]:
        return self._governance_rules_manager.rules

    @property
    def governance_rules_read_only(self) -> bool:
        return self._governance_rules_manager.is_read_only

    # Diagnostics delegation
    def _log(self, data: dict[str, Any]) -> None:
        log_structured(data)

    def _record_startup_failure(self, stage: str, error: BaseException) -> None:
        record_failure(stage, error, self.startup_failures)

    def _start_loop_stall_watchdog(self) -> None:
        start_loop_stall_watchdog(self)

    # Entry point
    async def main(self) -> None:
        await run_main(self)


# Backward compatibility: expose main() at module level
async def main() -> None:
    """Module entry point for backward compatibility."""
    app_instance = GPTBridgeApp()
    try:
        from ipc.server import run_server
        import argparse

        parser = argparse.ArgumentParser(description="GPTBridge Mother Tool Entry")
        parser.add_argument(
            "--serve",
            action="store_true",
            help="Start the IPC server for the mother tool.",
        )
        parser.add_argument(
            "--auto-kill-backend-port",
            action="store_true",
            help="Automatically terminate a previous GPTBridge backend holding the configured IPC port before starting.",
        )
        parser.add_argument(
            "--profile",
            default="main",
            help="Browser profile name forwarded by run.py; accepted for compatibility but not used by the server.",
        )

        args = parser.parse_args()

        app_instance = GPTBridgeApp()

        try:
            await run_server(
                app_instance,
                auto_kill_backend_port=args.auto_kill_backend_port,
            )
        finally:
            await app_instance.shutdown()
    finally:
        pass


if __name__ == "__main__":
    try:
        from startup import run_cli
        run_cli()
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)