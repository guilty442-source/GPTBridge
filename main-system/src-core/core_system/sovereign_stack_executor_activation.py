"""Sovereign stack activation mixin (A185 split).

Contains the activate() and deactivate() methods extracted from
SovereignStackExecutor.

A592/A604: the sub-sovereign layer is eliminated — activation covers the
five peer cores only; retired child identities are never materialized,
started or routed to (FORBID:sub-sovereign-routing).
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

    async def _start_top_sovereigns(self, sovereign: Any) -> None:
        raise NotImplementedError

    async def activate(self, sovereign: Any) -> bool:
        """Materialize and start the five peer sovereign cores."""
        app = self.app
        step_timings: dict[str, int] = {}
        _step_start = time.monotonic()

        self._startup_failures = []
        self._materialize_top_sovereigns(sovereign)
        await self._start_top_sovereigns(sovereign)

        # A592/A604: former sub-sovereign surfaces are retired lineage —
        # the compat attributes stay ``None`` by design, not by failure.
        app.maintenance_sovereign = None
        app.learning_system_sovereign = None
        app.system_programming_sovereign = None

        async def _start_cleaner() -> None:
            try:
                await app.daily_global_cleaner_service.start()
            except Exception as error:
                app._record_startup_failure("daily_global_cleaner", error)

        await asyncio.gather(_start_cleaner())
        step_timings["peer-sovereigns-and-cleaner_ms"] = int(
            (time.monotonic() - _step_start) * 1000
        )
        _step_start = time.monotonic()

        app._mark_startup_phase("maintenance_sovereign_starting")

        async def _start_maintenance() -> None:
            try:
                from core_system.resource_maintenance import release_unused_memory

                app.resource_release = release_unused_memory
                # A592/A604: the health-maintenance sub-sovereign identity is
                # retired — absent by design, not a fault.  Maintenance
                # responsibilities are absorbed by decision-core modules.
                app._log(
                    {
                        "type": "maintenance_sovereign_startup",
                        "skipped": "retired-A592-A604",
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
                automation_core=getattr(app, "automation_core", None),
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
            report = self._build_startup_report(sovereign)
            app._log(
                {
                    "type": "sovereign_startup",
                    "dependency_state": report.get("dependency_state", ""),
                }
            )
        except Exception as error:
            app._record_startup_failure("decision_sovereign", error)
        step_timings["decision-sovereign_ms"] = int(
            (time.monotonic() - _step_start) * 1000
        )
        app._startup_step_timings = step_timings
        app._log({"type": "startup_step_timings", **step_timings})
        app._mark_startup_phase("sovereign_initialized")
        return startup_ok

    def _build_startup_report(self, sovereign: Any) -> dict[str, Any]:
        """Persist the five-core activation report (A592/A604 shape)."""
        from core_system.sovereign_utils import _iso_now

        report = {
            "ok": len(self._startup_failures) == 0,
            "sovereign": "decision-sovereign",
            "dependency_state": sovereign._dependency_state(),
            "started_at": _iso_now(),
            "execution_delegation": "governed-executor-only",
            "startup_failures": list(self._startup_failures),
            "health_owner": "none-sub-sovereign-layer-eliminated-A592-A604",
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
        """Persist the stopped state (A592/A604: no children to stop)."""
        from core_system.sovereign_utils import _iso_now

        sovereign._save_state({"stopped_at": _iso_now()})


__all__ = ["SovereignStackActivationMixin"]
