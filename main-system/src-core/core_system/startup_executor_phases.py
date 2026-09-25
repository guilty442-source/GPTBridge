"""Startup executor — phase handlers mixin.

Extracted from StartupSovereignExecutor: the seven certified phase
handlers (phase-0 through phase-6) that perform decision/orchestration
level work during the startup generation.
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
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


async def _start_backup_scheduler_if_enabled(app: Any) -> None:
    """Assemble the single production BackupScheduler when explicitly enabled."""
    if os.environ.get("GPTBRIDGE_BACKUP_SCHEDULER", "0") != "1":
        return
    if getattr(app, "backup_scheduler", None) is not None:
        return
    from psycopg import Connection
    from shared_layer.database import DatabaseSettings
    from shared_layer.database.backup_scheduler import get_backup_scheduler
    from shared_layer.database.config import database_dsn
    from shared_layer.database.restore_certification import certify_restore

    settings = DatabaseSettings.from_environment()
    if not settings.admin_dsn.strip():
        raise RuntimeError("BACKUP_SCHEDULER_ADMIN_DSN_REQUIRED")

    def certify(_backup_path: str):
        with Connection.connect(database_dsn(settings.admin_dsn, settings.database)) as conn:
            return certify_restore(conn)

    scheduler = get_backup_scheduler(settings, restore_certifier=certify)
    app.backup_scheduler = scheduler
    # §1.1 自動化集中：automation core 持有節奏時不開私有 thread；
    # 拒絕註冊（kill switch）即不啟動私有迴圈。
    core = getattr(app, "automation_core", None)
    if core is not None:
        scheduler.start(spawn_loop=False)
        core.register_flow(
            "backup-scheduler",
            lambda: asyncio.to_thread(scheduler.run_once),
            interval_s=float(scheduler.check_interval),
        )
    else:
        scheduler.start()


class StartupExecutorPhasesMixin:
    """Phase handlers for StartupSovereignExecutor."""

    async def _phase_local_preflight(self, record: PhaseRecord) -> None:
        """PHASE-0: local preflight — canonical root and sealed layout only."""
        from pathlib import Path

        app = self.app  # type: ignore[attr-defined]
        root = Path(getattr(app, "project_root", "")).resolve()
        required = (
            root / "governance_rule",
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
            from tasks.task_queue import TaskQueue

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
        # A435: read the official codex identity through the official
        # entry bounded lookup — never a direct repository call.
        from governance_rule.execution.codex_reconcile import bounded_lookup

        identity = await asyncio.to_thread(
            bounded_lookup,
            "startup-executor",
            purpose="status",
            scope=("codex:identity",),
            reader=lambda ctx: ctx.codex_identity(),
        )
        integrity = False
        try:
            integrity = bool(app.governance.runtime_integrity_ready())
        except Exception:
            integrity = False
        if not integrity:
            raise RuntimeError("official-codex-integrity-unverified")
        record.detail["codex_version"] = identity["codex_version"]
        self._conditions["official-codex-valid"] = True  # type: ignore[attr-defined]

    async def _phase_load_permission_directory(self, record: PhaseRecord) -> None:
        """PHASE-3: load the permission directory; activate permission sovereign.

        Loads multiple permission snapshots in parallel for faster startup.
        """
        app = self.app  # type: ignore[attr-defined]

        # Load permission snapshots in parallel
        from governance_rule.permission_directory.code_rule_directory import (
            code_rule_directory_snapshot,
        )
        from governance_rule.permission_directory import directory_authority_snapshot

        from shared_layer.performance.thread_budget import bounded_workers

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor(
            max_workers=bounded_workers(2), thread_name_prefix="perm-snapshot"
        ) as executor:
            code_rule_future = loop.run_in_executor(executor, code_rule_directory_snapshot)
            dir_auth_future = loop.run_in_executor(executor, directory_authority_snapshot)

            directory = await code_rule_future
            dir_authority = await dir_auth_future

        if not directory.approved_tool_ids:
            raise RuntimeError("permission-directory-empty")

        record.detail["approved_tools"] = len(directory.approved_tool_ids)
        record.detail["dir_authority_version"] = getattr(
            dir_authority, "authority_version_policy", None
        )
        if app.permission_sovereign is None:
            from governance import PermissionSovereign

            app.permission_sovereign = PermissionSovereign(
                app, governance=app.governance
            )
        self._conditions["permission-sovereign-active"] = True  # type: ignore[attr-defined]

    async def _phase_switch_normal_information_mode(
        self, record: PhaseRecord
    ) -> None:
        """PHASE-4: normal information mode — channel + toolbox + status."""
        timings: dict[str, float] = {}
        mark = time.monotonic()

        def _lap(name: str) -> None:
            nonlocal mark
            timings[name] = round((time.monotonic() - mark) * 1000, 1)
            mark = time.monotonic()

        app = self.app  # type: ignore[attr-defined]
        if app.toolbox_service is None:
            from tasks.toolbox_service import ToolboxService

            app.toolbox_service = ToolboxService(
                app.project_root,
                governance=app.governance,
                permission_sovereign=app.permission_sovereign,
            )
        _lap("toolbox_construct_ms")
        await asyncio.to_thread(app.toolbox_service.reconcile_process_registry)
        _lap("registry_reconcile_ms")
        await app.toolbox_service.start_process_registry_monitor(
            automation_core=getattr(app, "automation_core", None)
        )
        _lap("registry_monitor_ms")
        await _start_backup_scheduler_if_enabled(app)
        _lap("backup_scheduler_ms")
        if getattr(app, "model_service_activation", None) is None:
            from tasks.model_service_activation import (
                ModelServiceActivationBroker,
            )

            app.model_service_activation = ModelServiceActivationBroker(
                app,
                app.toolbox_service,
                project_root=app.project_root,
            )
            await app.model_service_activation.start()
        _lap("model_activation_ms")
        if getattr(app, "self_learning_driver", None) is None:
            from tasks.self_learning_driver import SelfLearningDriver

            # A554/§1.1：星澄 self-learning 排程——經 AutomationCore 註冊
            # 到共享排程（deny 不回落私有迴圈）；循環本身在工具行程內
            # 經 governed system channel 執行（inference_exclusion 需要
            # 行程本地 engine cache 才有效）。
            app.self_learning_driver = SelfLearningDriver(
                app,
                app.toolbox_service,
                project_root=app.project_root,
            )
            await app.self_learning_driver.start()
        _lap("self_learning_driver_ms")
        if getattr(app, "codex_amendment_intake", None) is None:
            from tasks.codex_amendment_intake import (
                CodexAmendmentIntakeDriver,
            )

            # A382/A488 + §1.1：修訂 intake 驅動——掃描 staged request
            # artifact → successor build → 五核心稽核 → 停在
            # ready-for-governor（發布仍為總督 --apply）。xingcheng
            # 收據的 governed web-search 只在 local-model 運行時接線；
            # 冷停時 defer 而非永久拒絕。
            app.codex_amendment_intake = CodexAmendmentIntakeDriver(
                app,
                app.toolbox_service,
                project_root=app.project_root,
            )
            await app.codex_amendment_intake.start()
        _lap("codex_amendment_intake_ms")
        if getattr(app, "sleep_policy", None) is None:
            from tasks.sleep_policy import SleepPolicyManager

            app.sleep_policy = SleepPolicyManager(app, app.toolbox_service)
            await app.sleep_policy.start()
            # On-demand executions are demand signals for the idle
            # manager — the callback was declared on ToolboxService but
            # never wired, so requests could not refresh the baseline.
            app.toolbox_service._tool_activity_callback = (
                app.sleep_policy.note_activity
            )
        _lap("sleep_policy_ms")
        if getattr(app, "git_automation", None) is None:
            from tasks.git_automation import GitAutomationService

            app.git_automation = GitAutomationService(
                app.project_root,
                scheduler=getattr(app, "periodic_scheduler", None),
                automation_core=getattr(app, "automation_core", None),
            )
            await app.git_automation.start()
        _lap("git_automation_ms")
        if getattr(app, "resource_mode_advisor", None) is None:
            from tasks.resource_mode_advisor import ResourceModeAdvisor

            app.resource_mode_advisor = ResourceModeAdvisor(
                app.project_root,
                scheduler=getattr(app, "periodic_scheduler", None),
                automation_core=getattr(app, "automation_core", None),
            )
            await app.resource_mode_advisor.start()
        _lap("resource_mode_advisor_ms")
        if getattr(app, "saga_runtime", None) is None:
            from core_system.saga_runtime_integration import (
                create_saga_runtime_integration,
            )

            app.saga_runtime = create_saga_runtime_integration(app)
            await app.saga_runtime.start()
        _lap("saga_runtime_ms")
        if app.runtime_status_service is None:
            from tasks.runtime_status_service import RuntimeStatusService

            app.runtime_status_service = RuntimeStatusService(app)
        if getattr(app, "command_router", None) is None:
            await app.runtime_bootstrap.initialize_main()
        _lap("initialize_main_ms")
        record.detail["timings_ms"] = timings
        # The state-change notifier (created when the listener bound) is the
        # normal-mode information channel; absence means the listener never
        # came up — fail closed rather than proceed deaf.
        if getattr(app, "_state_change_notifier", None) is None:
            raise RuntimeError("normal-information-layer-unavailable")
        self._conditions["normal-information-layer-active"] = True  # type: ignore[attr-defined]

    async def _phase_classify_dependency_dag(self, record: PhaseRecord) -> None:
        """PHASE-5: build + verify the certified dependency DAG (E155/A191)."""
        timings: dict[str, float] = {}
        mark = time.monotonic()
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
        timings["dag_ms"] = round((time.monotonic() - mark) * 1000, 1)
        mark = time.monotonic()
        from shared_layer.service_probe import probe_registered_local_service

        timings["probe_import_ms"] = round((time.monotonic() - mark) * 1000, 1)
        mark = time.monotonic()

        # Bounded-concurrent probes (§10.63 R1): unreachable dependencies
        # burn the full timeout each — serial probing stacks those waits.
        # gather preserves declaration order; the fail-closed verdict is
        # unchanged.
        probe_slots = asyncio.Semaphore(4)

        async def _probe(identity: str):
            async with probe_slots:
                return await asyncio.to_thread(
                    probe_registered_local_service, identity, timeout=0.75
                )

        probes = await asyncio.gather(
            *(_probe(dep.identity) for dep in dag.dependencies)
        )
        timings["probes_ms"] = round((time.monotonic() - mark) * 1000, 1)
        record.detail["timings_ms"] = timings
        dependency_evidence: dict[str, Any] = {}
        core_ready = True
        for dep, probe in zip(dag.dependencies, probes):
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
