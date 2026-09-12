"""Sovereign Stack Executor — 受管執行器（物質化與啟動整套主宰堆疊）。

法典依據:
- A63 (→A128/A130): SOVEREIGN: decision-only;
  EXECUTION:delegated-to-governed-executor
- A130: SUB-SOVEREIGN: control/dispatch under parent authority;
  EXECUTION:governed-executor
- A334: ``sovereign_hierarchy_registry`` is the machine authority for each
  sub-sovereign's single parent; ``module_assignment_registry`` is the
  machine authority for each executable module's managing sub-sovereign.
  Both are enforced here at materialization/dispatch time (fail-closed).
- A128/A130: the mother process delegates sovereign-stack startup to the
  decision-sovereign, which adjudicates the dispatch; THIS executor performs
  the actual materialization and activation work.

The decision-sovereign decides the dispatch order and authorizes the
startup; this governed executor executes it:

  1. Materialize every active registry child under its codex-registered
     parent (A334 single-parent map):
       system-runtime-sovereign  -> system-sub, startup-sub
       permission-sovereign      -> language-review, directory, identity-group
       decision-sovereign        -> policy-architecture, health-maintenance-test,
                                    data-governance, priority-capability,
                                    change-acceptance
       synchronization-sovereign -> resource-dependency-sync, dependency-sync,
                                    channel-contract-sync, release-update-sync,
                                    learning-evidence-sync, runtime-state-sync,
                                    repair-backup-sync, cleanup-retention-sync,
                                    automatic-log-sync
  2. Start each child ONLY after its codex parent authorizes the activation
     (``parent.authorize_child_activation`` — decision-layer adjudication;
     the actual ``child.start()`` is executor work).

All heavy work stays delegated to governed executors; nothing here is a
sovereign — this module is a plain orchestration executor owned by the
startup path.
"""

from __future__ import annotations

import asyncio
import time
from importlib import import_module
from typing import Any

from governance.registries import children_of, validate_child_parent


def _sub_sovereigns_module() -> Any:
    return import_module("governance.sub_sovereigns")


# child_identity -> governance.sub_sovereigns class name
_CHILD_CLASSES: dict[str, str] = {
    "system-sub-sovereign": "SystemSubSovereign",
    "startup-sub-sovereign": "StartupSubSovereign",
    "language-review-sub-sovereign": "LanguageReviewSubSovereign",
    "directory-sub-sovereign": "DirectorySubSovereign",
    "identity-group-sub-sovereign": "IdentityGroupSubSovereign",
    "policy-architecture-sub-sovereign": "PolicyArchitectureSubSovereign",
    "health-maintenance-test-sub-sovereign": "HealthMaintenanceTestSubSovereign",
    "data-governance-sub-sovereign": "DataGovernanceSubSovereign",
    "priority-capability-sub-sovereign": "PriorityCapabilitySubSovereign",
    "change-acceptance-sub-sovereign": "ChangeAcceptanceSubSovereign",
    "resource-dependency-sync-sub-sovereign": "ResourceDependencySyncSubSovereign",
    "dependency-sync-sub-sovereign": "DependencySyncSubSovereign",
    "channel-contract-sync-sub-sovereign": "ChannelContractSyncSubSovereign",
    "release-update-sync-sub-sovereign": "ReleaseUpdateSyncSubSovereign",
    "learning-evidence-sync-sub-sovereign": "LearningEvidenceSyncSubSovereign",
    "runtime-state-sync-sub-sovereign": "RuntimeStateSyncSubSovereign",
    "repair-backup-sync-sub-sovereign": "RepairBackupSyncSubSovereign",
    "cleanup-retention-sync-sub-sovereign": "CleanupRetentionSyncSubSovereign",
    "automatic-log-sync-sub-sovereign": "AutomaticLogSyncSubSovereign",
}

# Per-child start kwargs resolved at dispatch time.
def _child_start_kwargs(app: Any, child_id: str) -> dict[str, Any]:
    if child_id == "resource-dependency-sync-sub-sovereign":
        return {"memory_maintainer": getattr(app, "_idle_memory_maintainer", None)}
    return {}


class SovereignStackExecutor:
    """Governed executor: materializes and activates the sovereign stack."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._startup_failures: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # A334 materialization: every child under its codex-registered parent
    # ------------------------------------------------------------------

    def _materialize_top_sovereigns(self, sovereign: Any) -> None:
        """Ensure the other top-level sovereigns exist before child
        materialization (children resolve their parent through the app)."""
        app = self.app
        top_specs = (
            (
                "permission_sovereign",
                "governance.sovereigns.permission_sovereign",
                "PermissionSovereign",
            ),
            (
                "synchronization_sovereign",
                "governance.sovereigns.synchronization_sovereign",
                "SynchronizationSovereign",
            ),
            (
                "system_runtime_sovereign",
                "governance.sovereigns.system_runtime_sovereign",
                "SystemRuntimeSovereign",
            ),
        )
        for attr_name, module_name, class_name in top_specs:
            if getattr(app, attr_name, None) is not None:
                continue
            try:
                cls = getattr(import_module(module_name), class_name)
                if attr_name == "permission_sovereign":
                    setattr(
                        app,
                        attr_name,
                        cls(app, governance=getattr(app, "governance", None)),
                    )
                else:
                    setattr(app, attr_name, cls(app))
            except Exception as error:
                self._startup_failures.append(
                    {
                        "sub_sovereign": attr_name,
                        "error": f"top-sovereign-materialize:{type(error).__name__}: {error}",
                    }
                )

    async def _start_top_sovereigns(self, sovereign: Any) -> None:
        """Activate the top-level coordination sovereigns (decision-layer
        surfaces; sub-sovereign dispatch is still executor work)."""
        app = self.app
        for top in (
            getattr(app, "system_runtime_sovereign", None),
            getattr(app, "permission_sovereign", None),
            getattr(app, "synchronization_sovereign", None),
        ):
            if top is not None and not getattr(top, "_started", False):
                try:
                    await top.start()
                except Exception as error:
                    self._startup_failures.append(
                        {
                            "sub_sovereign": getattr(top, "sovereign_id", "?"),
                            "error": f"top-sovereign:{type(error).__name__}: {error}",
                        }
                    )

    def _parent_object(self, sovereign: Any, parent_id: str) -> Any:
        """Resolve a registered parent identity to the live sovereign."""
        app = self.app
        return {
            "decision-sovereign": sovereign,
            "permission-sovereign": getattr(app, "permission_sovereign", None),
            "synchronization-sovereign": getattr(
                app, "synchronization_sovereign", None
            ),
            "system-runtime-sovereign": getattr(
                app, "system_runtime_sovereign", None
            ),
        }.get(parent_id)

    def _materialize_children(self, sovereign: Any) -> None:
        """Instantiate every active registry child under its codex parent.

        Fail-closed: a child whose registry parent is missing or mismatched
        is recorded as a startup failure and never materialized.
        """
        app = self.app
        sub = _sub_sovereigns_module()
        for child_id, class_name in _CHILD_CLASSES.items():
            parent_id = self._codex_parent(child_id)
            parent = self._parent_object(sovereign, parent_id) if parent_id else None
            if parent is None:
                self._startup_failures.append(
                    {
                        "sub_sovereign": child_id,
                        "error": f"codex-parent-unavailable:{parent_id}",
                    }
                )
                continue
            registry = getattr(parent, "_sub_sovereigns", None)
            if registry is None:
                continue
            if child_id not in registry:
                try:
                    child_cls = getattr(sub, class_name)
                    registry[child_id] = child_cls(app, parent=parent)
                except Exception as error:
                    self._startup_failures.append(
                        {
                            "sub_sovereign": child_id,
                            "error": f"materialize:{type(error).__name__}: {error}",
                        }
                    )

        app_registry = getattr(app, "_sub_sovereigns", None)
        if isinstance(app_registry, dict):
            for parent in {
                sovereign,
                getattr(app, "permission_sovereign", None),
                getattr(app, "synchronization_sovereign", None),
                getattr(app, "system_runtime_sovereign", None),
            }:
                if parent is not None:
                    app_registry.update(getattr(parent, "_sub_sovereigns", {}))

    @staticmethod
    def _codex_parent(child_id: str) -> str | None:
        from governance.registries import parent_of

        return parent_of(child_id)

    async def _start_child(
        self, sovereign: Any, tag: str, child_id: str
    ) -> dict[str, Any]:
        """Parent-authorized child activation (A334 + governed execution)."""
        parent_id = self._codex_parent(child_id)
        parent = self._parent_object(sovereign, parent_id) if parent_id else None
        child = None
        if parent is not None:
            child = getattr(parent, "_sub_sovereigns", {}).get(child_id)
        if child is None:
            self._startup_failures.append(
                {"sub_sovereign": tag, "error": f"not-materialized:{child_id}"}
            )
            return {}
        outcome = parent.authorize_child_activation(child_id)
        if not outcome.accepted:
            reason = outcome.refusal.reason_code if outcome.refusal else "REFUSED"
            self._startup_failures.append(
                {"sub_sovereign": tag, "error": f"parent-authorization:{reason}"}
            )
            return {}
        try:
            return await child.start(
                **_child_start_kwargs(self.app, child_id)
            )
        except Exception as error:
            self._startup_failures.append(
                {"sub_sovereign": tag, "error": f"{type(error).__name__}: {error}"}
            )
            return {}

    # ------------------------------------------------------------------
    # Activation sequence
    # ------------------------------------------------------------------

    async def activate(self, sovereign: Any) -> bool:
        """Materialize and start the entire sovereign stack.

        ``sovereign`` is the decision-sovereign that authorized this
        dispatch; the executor registers materialized children under their
        codex parents and the app's ``_sub_sovereigns`` surface.  Returns the
        maintenance_ready flag.
        """

        app = self.app
        step_timings: dict[str, int] = {}
        _step_start = time.monotonic()

        # Materialize top-level sovereigns first (children resolve their
        # codex parent through the app), then every active registry child
        # under its codex parent (A334).
        self._startup_failures = []
        self._materialize_top_sovereigns(sovereign)
        self._materialize_children(sovereign)
        await self._start_top_sovereigns(sovereign)

        # The health-maintenance sub-sovereign is a decision-sovereign child
        # (A302/A323); surface it on the app under its legacy attribute for
        # existing callers (ipc handlers, repair chains).
        app.maintenance_sovereign = sovereign._sub_sovereigns.get(
            "health-maintenance-test-sub-sovereign"
        )
        # Legacy app attributes for the synchronization children used by
        # repair/learning chains.
        synchronization = getattr(app, "synchronization_sovereign", None)
        sync_children = (
            getattr(synchronization, "_sub_sovereigns", {})
            if synchronization is not None
            else {}
        )
        app.learning_system_sovereign = sync_children.get(
            "learning-evidence-sync-sub-sovereign"
        )
        app.system_programming_sovereign = sync_children.get(
            "release-update-sync-sub-sovereign"
        )

        # Learning-evidence and release-update start before maintenance so
        # every subsequent failure and repair can be learned, and every code
        # change has one governed dispatch owner.  The daily cleaner is
        # independent — E155 bounded-independent-parallelism applies.
        async def _start_cleaner() -> None:
            try:
                await app.daily_global_cleaner_service.start()
            except Exception as error:
                app._record_startup_failure("daily_global_cleaner", error)

        early_starts = [
            self._start_child(
                sovereign, "learning", "learning-evidence-sync-sub-sovereign"
            ),
            self._start_child(
                sovereign, "programming", "release-update-sync-sub-sovereign"
            ),
            _start_cleaner(),
        ]
        await asyncio.gather(*early_starts)
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
                maintenance = app.maintenance_sovereign
                if maintenance is None:
                    raise RuntimeError("health-maintenance-test-sub-sovereign-unavailable")
                outcome = sovereign.authorize_child_activation(
                    "health-maintenance-test-sub-sovereign"
                )
                if not outcome.accepted:
                    reason = (
                        outcome.refusal.reason_code if outcome.refusal else "REFUSED"
                    )
                    raise RuntimeError(f"parent-authorization:{reason}")
                maintenance_report = await maintenance.start(
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
        #    (materialized above in _materialize_top_sovereigns)
        app._mark_startup_phase("permission_sovereign_starting")
        try:
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
        #    start, so both run under E155 bounded-independent-parallelism.
        app._mark_startup_phase("sovereign_initializing")
        try:
            from core_system.main_system_self_maintenance import (
                MainSystemSelfMaintenance,
            )

            app.main_system_self_maintenance = MainSystemSelfMaintenance(
                sovereign.workspace_root,
                authentication=getattr(app.governance, "authentication", None),
            )
        except Exception as error:
            app.main_system_self_maintenance = None
            app._record_startup_failure("main_system_self_maintenance", error)

        async def _start_self_maintenance() -> None:
            if app.main_system_self_maintenance is None:
                return
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

        # 4. Decision Sovereign activation + remaining registry children,
        #    each under its codex parent's authorization (A334).
        try:
            await sovereign.start()
            report = await self._start_children(sovereign)
            app._log(
                {
                    "type": "sovereign_startup",
                    "dependency_state": report.get("dependency_state", ""),
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

    async def _start_children(self, sovereign: Any) -> dict[str, Any]:
        """Start the remaining registry children under parent authorization.

        Single-fault isolation: each sub-sovereign is started independently.
        A failure in one does not prevent the rest from starting, and all
        failures are recorded in the report's ``startup_failures`` list.
        """

        from core_system.sovereign_utils import _iso_now

        dependency_state = sovereign._dependency_state()

        runtime, resource, data, integration, language_review, third_party = (
            await asyncio.gather(
                self._start_child(
                    sovereign, "runtime", "runtime-state-sync-sub-sovereign"
                ),
                self._start_child(
                    sovereign, "resource", "resource-dependency-sync-sub-sovereign"
                ),
                self._start_child(
                    sovereign, "data", "data-governance-sub-sovereign"
                ),
                self._start_child(
                    sovereign, "integration", "channel-contract-sync-sub-sovereign"
                ),
                self._start_child(
                    sovereign, "language_review", "language-review-sub-sovereign"
                ),
                self._start_child(
                    sovereign, "third_party", "dependency-sync-sub-sovereign"
                ),
            )
        )

        # A334 completeness: activate every remaining codex-registered child
        # that was materialized but not yet started, under its codex
        # parent's authorization.
        remaining: list[Any] = []
        for parent_id in (
            "system-runtime-sovereign",
            "permission-sovereign",
            "decision-sovereign",
            "synchronization-sovereign",
        ):
            parent = self._parent_object(sovereign, parent_id)
            if parent is None:
                continue
            for cid in children_of(parent_id):
                child = getattr(parent, "_sub_sovereigns", {}).get(cid)
                if child is not None and not getattr(child, "_started", False):
                    remaining.append(self._start_child(sovereign, cid, cid))
        if remaining:
            await asyncio.gather(*remaining)

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

        synchronization = getattr(self.app, "synchronization_sovereign", None)
        sync_children = (
            getattr(synchronization, "_sub_sovereigns", {})
            if synchronization is not None
            else {}
        )
        learning = sync_children.get("learning-evidence-sync-sub-sovereign")
        programming = sync_children.get("release-update-sync-sub-sovereign")

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
                "learning": getattr(learning, "_started", False),
                "programming": getattr(programming, "_started", False),
            },
            "health_owner": "health-maintenance-test-sub-sovereign",
            "sources": [
                {"kind": "env", "name": "GPTBRIDGE_STARTUP_STATE"},
                {
                    "kind": "report",
                    "path": str(sovereign.launcher_report_path),
                },
            ],
        }
        sovereign._save_state(report)
        return report

    async def deactivate(self, sovereign: Any) -> None:
        """Stop every materialized registry child (reverse order).

        The health-maintenance sub-sovereign and permission sovereign are
        stopped by the app.
        """
        from core_system.sovereign_utils import _iso_now

        for parent in (
            getattr(self.app, "synchronization_sovereign", None),
            getattr(self.app, "permission_sovereign", None),
            sovereign,
            getattr(self.app, "system_runtime_sovereign", None),
        ):
            if parent is None:
                continue
            for child in list(getattr(parent, "_sub_sovereigns", {}).values()):
                try:
                    await child.stop()
                except Exception:
                    pass
        sovereign._save_state({"stopped_at": _iso_now()})


__all__ = ["SovereignStackExecutor"]
