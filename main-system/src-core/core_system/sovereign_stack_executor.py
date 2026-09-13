"""Sovereign Stack Executor — facade.

This module provides the SovereignStackExecutor class.  Constants
and helpers live in :mod:`core_system.sovereign_stack_executor_constants`.

Governed executor: materializes and activates the sovereign stack
(A63/A128/A130/A334).
"""

from __future__ import annotations

import asyncio
import time
from importlib import import_module
from typing import Any

from governance.registries import children_of, validate_child_parent

from .sovereign_stack_executor_constants import (
    _sub_sovereigns_module,
    _CHILD_CLASSES,
    _child_start_kwargs,
)
from .sovereign_stack_executor_children import SovereignStackChildrenMixin


class SovereignStackExecutor(SovereignStackChildrenMixin):
    """Governed executor: materializes and activates the sovereign stack."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self._startup_failures: list[dict[str, str]] = []

    # ------------------------------------------------------------------
    # A334 materialization: every child under its codex-registered parent
    # ------------------------------------------------------------------

    def _materialize_top_sovereigns(self, sovereign: Any) -> None:
        """Ensure the other top-level sovereigns exist before child materialization."""
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
        """Activate the top-level coordination sovereigns."""
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
        """Instantiate every active registry child under its codex parent."""
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
            result = await child.start(
                **_child_start_kwargs(self.app, child_id)
            )
            try:
                parent.record_child_success(child_id)
            except Exception:
                pass
            return result
        except Exception as error:
            try:
                parent.record_child_failure(child_id)
            except Exception:
                pass
            self._startup_failures.append(
                {"sub_sovereign": tag, "error": f"{type(error).__name__}: {error}"}
            )
            return {}

    # ------------------------------------------------------------------
    # Governed single-child restart (decision-layer autonomy entry point)
    # ------------------------------------------------------------------

    async def restart_child(
        self, sovereign: Any, child_id: str
    ) -> dict[str, Any]:
        """Restart one materialized child under codex-parent authorization."""
        parent_id = self._codex_parent(child_id)
        parent = (
            self._parent_object(sovereign, parent_id) if parent_id else None
        )
        if parent is None:
            return {
                "ok": False,
                "child": child_id,
                "error": f"codex-parent-unavailable:{parent_id}",
            }
        registry = getattr(parent, "_sub_sovereigns", None)
        if registry is None:
            return {
                "ok": False,
                "child": child_id,
                "error": "parent-registry-unavailable",
            }
        child = registry.get(child_id)
        if child is None:
            class_name = _CHILD_CLASSES.get(child_id)
            if class_name is None:
                return {
                    "ok": False,
                    "child": child_id,
                    "error": f"unknown-child:{child_id}",
                }
            try:
                child_cls = getattr(_sub_sovereigns_module(), class_name)
                registry[child_id] = child_cls(self.app, parent=parent)
            except Exception as error:
                return {
                    "ok": False,
                    "child": child_id,
                    "error": f"rematerialize:{type(error).__name__}: {error}",
                }
        await self._start_child(sovereign, child_id, child_id)
        child = registry.get(child_id)
        return {
            "ok": bool(getattr(child, "_started", False)),
            "child": child_id,
            "parent": parent_id,
        }

    # ------------------------------------------------------------------
    # Activation sequence
    # ------------------------------------------------------------------

    async def activate(self, sovereign: Any) -> bool:
        """Materialize and start the entire sovereign stack."""
        app = self.app
        step_timings: dict[str, int] = {}
        _step_start = time.monotonic()

        self._startup_failures = []
        self._materialize_top_sovereigns(sovereign)
        self._materialize_children(sovereign)
        await self._start_top_sovereigns(sovereign)

        app.maintenance_sovereign = sovereign._sub_sovereigns.get(
            "health-maintenance-test-sub-sovereign"
        )
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
        startup_ok = bool(
            getattr(app.main_system_self_maintenance, "_running", False)
        )
        app.maintenance_ready = startup_ok
        if app.governance is not None:
            app.governance.maintenance_ready = startup_ok

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

    async def deactivate(self, sovereign: Any) -> None:
        """Stop every materialized registry child (reverse order)."""
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
