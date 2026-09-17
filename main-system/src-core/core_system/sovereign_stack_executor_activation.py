"""Sovereign stack activation mixin (A185 split).

Contains the activate() and deactivate() methods extracted from
SovereignStackExecutor.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class SovereignStackActivationMixin:
    """Sovereign stack activation and deactivation."""

    app: Any
    _startup_failures: list[dict[str, str]]

    def _materialize_top_sovereigns(self, sovereign: Any) -> None:
        raise NotImplementedError

    def _materialize_children(self, sovereign: Any) -> None:
        raise NotImplementedError

    async def _start_top_sovereigns(self, sovereign: Any) -> None:
        raise NotImplementedError

    async def _start_child(
        self, sovereign: Any, tag: str, child_id: str
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def _start_children(self, sovereign: Any) -> dict[str, Any]:
        raise NotImplementedError

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
        automation = getattr(app, "automation_sovereign", None)
        sync_children = (
            getattr(automation, "_sub_sovereigns", {})
            if automation is not None
            else {}
        )
        # A485: the learning sub-sovereign is a privileged-institution-managed
        # child of 星澄 — never of the automation/synchronization family.
        xingcheng = getattr(app, "xingcheng_sovereign", None)
        app.learning_system_sovereign = (
            getattr(xingcheng, "_sub_sovereigns", {}).get(
                "learning-evidence-sync-sub-sovereign"
            )
            if xingcheng is not None
            else None
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
            step_timings["decision-sovereign-start_ms"] = int(
                (time.monotonic() - _step_start) * 1000
            )
            _children_start = time.monotonic()
            report = await self._start_children(sovereign)
            step_timings["decision-children_ms"] = int(
                (time.monotonic() - _children_start) * 1000
            )
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
        app._log({"type": "startup_step_timings", **step_timings})
        app._mark_startup_phase("sovereign_initialized")
        return startup_ok

    async def deactivate(self, sovereign: Any) -> None:
        """Stop every materialized registry child (reverse order)."""
        from core_system.sovereign_utils import _iso_now

        for parent in (
            getattr(self.app, "automation_sovereign", None),
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


__all__ = ["SovereignStackActivationMixin"]
