"""Startup executor — phase handlers mixin.

Extracted from StartupSovereignExecutor: the seven certified phase
handlers (phase-0 through phase-6) that perform decision/orchestration
level work during the startup generation.
"""

from __future__ import annotations

import asyncio
from typing import Any

from startup_core.startup_config import (
    dependency_manifest as _cfg_dependency_manifest,
)

from .governed_startup_types import DependencyDeclaration
from .governed_startup_verify import (
    DependencyDAG,
    verify_dependency_classification,
)
from .startup_executor_types import PhaseRecord


class StartupExecutorPhasesMixin:
    """Phase handlers for StartupSovereignExecutor."""

    async def _phase_local_preflight(self, record: PhaseRecord) -> None:
        """PHASE-0: local preflight — canonical root and sealed layout only."""
        from pathlib import Path

        app = self.app  # type: ignore[attr-defined]
        root = Path(getattr(app, "project_root", "")).resolve()
        required = (
            root / "governance_rule",
            root / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3",
            root / "main-system" / "src-core" / "main.py",
            root / "shared-layer" / "src",
        )
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            raise RuntimeError(f"preflight-missing:{','.join(missing)}")
        record.detail["project_root"] = str(root)
        self._conditions["local-preflight-ok"] = True  # type: ignore[attr-defined]

    async def _phase_minimal_information_bootstrap(
        self, record: PhaseRecord
    ) -> None:
        """PHASE-1: minimal information bootstrap (last-valid-sealed state)."""
        app = self.app  # type: ignore[attr-defined]
        if app.task_queue is None:
            from tasks.queue import TaskQueue

            app.task_queue = TaskQueue(app.project_root, app.core_logger)
        # A67: repair coordinator prevents duplicate repair owners.
        from tasks.repair_coordinator import init_repair_coordinator

        init_repair_coordinator(app.project_root)
        self._conditions["minimal-information-bootstrap"] = True  # type: ignore[attr-defined]

    async def _phase_read_official_codex(self, record: PhaseRecord) -> None:
        """PHASE-2: read the official codex and verify runtime integrity."""
        app = self.app  # type: ignore[attr-defined]
        if app.governance is None:
            from core_system.governance_runtime import MainSystemGovernance

            app.governance = MainSystemGovernance.from_environment(app.project_root)
        from governance_rule.execution.codex_repository import load_governance_codex

        codex = await asyncio.to_thread(load_governance_codex)
        integrity = False
        try:
            integrity = bool(app.governance.runtime_integrity_ready())
        except Exception:
            integrity = False
        if not integrity:
            raise RuntimeError("official-codex-integrity-unverified")
        record.detail["codex_version"] = codex.codex_version
        self._conditions["official-codex-valid"] = True  # type: ignore[attr-defined]

    async def _phase_load_permission_directory(self, record: PhaseRecord) -> None:
        """PHASE-3: load the permission directory; activate permission sovereign."""
        app = self.app  # type: ignore[attr-defined]
        from governance_rule.permission_directory.code_rule_directory import (
            code_rule_directory_snapshot,
        )

        directory = code_rule_directory_snapshot()
        if not directory.approved_tool_ids:
            raise RuntimeError("permission-directory-empty")
        if app.permission_sovereign is None:
            from governance import PermissionSovereign

            app.permission_sovereign = PermissionSovereign(
                app, governance=app.governance
            )
        record.detail["approved_tools"] = len(directory.approved_tool_ids)
        self._conditions["permission-sovereign-active"] = True  # type: ignore[attr-defined]

    async def _phase_switch_normal_information_mode(
        self, record: PhaseRecord
    ) -> None:
        """PHASE-4: normal information mode — channel + toolbox + status."""
        app = self.app  # type: ignore[attr-defined]
        if app.toolbox_service is None:
            from tasks.toolbox_service import ToolboxService

            app.toolbox_service = ToolboxService(
                app.project_root,
                governance=app.governance,
                permission_sovereign=app.permission_sovereign,
            )
        if app.runtime_status_service is None:
            from tasks.runtime_status_service import RuntimeStatusService

            app.runtime_status_service = RuntimeStatusService(app)
        if getattr(app, "command_router", None) is None:
            await app.runtime_bootstrap.initialize_main()
        # The state-change notifier (created when the listener bound) is the
        # normal-mode information channel; absence means the listener never
        # came up — fail closed rather than proceed deaf.
        if getattr(app, "_state_change_notifier", None) is None:
            raise RuntimeError("normal-information-layer-unavailable")
        self._conditions["normal-information-layer-active"] = True  # type: ignore[attr-defined]

    async def _phase_classify_dependency_dag(self, record: PhaseRecord) -> None:
        """PHASE-5: build + verify the certified dependency DAG (E155/A191)."""
        declarations = tuple(
            DependencyDeclaration(**entry) for entry in _cfg_dependency_manifest()
        )
        dag = DependencyDAG(dependencies=declarations)
        if not dag.is_acyclic:
            raise RuntimeError("dependency-dag-cycle-rejected")
        check = verify_dependency_classification(dag)
        if not check["ok"]:
            raise RuntimeError(
                "dependency-classification-violation:"
                + ",".join(check["violations"])
            )
        from shared_layer.service_probe import probe_registered_local_service

        dependency_evidence: dict[str, Any] = {}
        core_ready = True
        for dep in dag.dependencies:
            probe = await asyncio.to_thread(
                probe_registered_local_service, dep.identity, timeout=0.75
            )
            dependency_evidence[dep.identity] = {
                "criticality": dep.criticality,
                "reachable": probe.reachable,
                "readiness_contract": dep.readiness_contract,
            }
            if dep.is_core_critical and not probe.reachable:
                core_ready = False
        if not core_ready:
            unreachable = [
                name
                for name, ev in dependency_evidence.items()
                if ev["criticality"] == "core-critical" and not ev["reachable"]
            ]
            raise RuntimeError(
                "core-critical-dependency-unreachable:" + ",".join(unreachable)
            )
        record.detail["dependencies"] = dependency_evidence
        record.detail["degraded_capabilities"] = [
            dep.required_by
            for dep in dag.dependencies
            if dep.is_capability_critical
            and not dependency_evidence[dep.identity]["reachable"]
        ]
        self._dependency_evidence = dependency_evidence  # type: ignore[attr-defined]
        self._conditions["all-core-critical-dependencies-ready"] = core_ready  # type: ignore[attr-defined]

    async def _phase_activate_core_sovereigns(self, record: PhaseRecord) -> None:
        """PHASE-6: activate core sovereigns with bounded parallelism (E155)."""
        app = self.app  # type: ignore[attr-defined]
        started = await app.decision_sovereign.start_sovereign_stack()
        if not started:
            raise RuntimeError("sovereign-activation-failed")
        self._activated.append(  # type: ignore[attr-defined]
            ("sovereign-stack", app.decision_sovereign.stop)
        )
        self._conditions.update(  # type: ignore[attr-defined]
            {
                "decision-active": True,
                "system-runtime-active": True,
                "maintenance-active": bool(
                    getattr(app, "maintenance_ready", False)
                ),
            }
        )
