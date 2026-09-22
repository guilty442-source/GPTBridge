"""GPTBridgeApp shutdown mixin.

Extracted from main.py: the shutdown and _shutdown_once methods that
gracefully stop all sovereigns, services, and tool backends within
a bounded deadline.
"""

from __future__ import annotations

import asyncio
import time


class GPTBridgeAppShutdownMixin:
    """Shutdown methods for GPTBridgeApp."""

    async def shutdown(self) -> None:
        if self._shutdown_started:  # type: ignore[attr-defined]
            await self._shutdown_complete.wait()  # type: ignore[attr-defined]
            return
        self._shutdown_started = True  # type: ignore[attr-defined]
        try:
            # Complete-close bound: a stalled tool/sovereign/service stop must
            # never keep the backend alive after the UI has closed.  The
            # deadline sits inside the launcher-side graceful window so the
            # supervisor observes a clean exit instead of a force-kill.
            try:
                await asyncio.wait_for(self._shutdown_once(), timeout=8.0)  # type: ignore[attr-defined]
            except asyncio.TimeoutError:
                self._log(  # type: ignore[attr-defined]
                    {
                        "type": "warning",
                        "message": (
                            "shutdown deadline exceeded; abandoning remaining "
                            "cleanup so the backend can exit"
                        ),
                    }
                )
        finally:
            self._shutdown_complete.set()  # type: ignore[attr-defined]

    async def _shutdown_once(self) -> None:
        backup_scheduler = getattr(self, "backup_scheduler", None)
        if backup_scheduler is not None:
            try:
                backup_scheduler.stop()
            except Exception:
                pass
            self.backup_scheduler = None

        # Close tools owned by this main system before stopping its health
        # monitor.  ToolboxService filters by process ownership and manifest,
        # so independent standalone tools remain running.
        try:
            toolbox = getattr(self, "toolbox_service", None)
            if toolbox is not None:
                await toolbox.stop_process_registry_monitor()
                await toolbox.shutdown_managed_tools()
        except Exception:
            pass

        # Stop the tool isolation health monitor next — every remaining tool
        # exit from this point on is an intentional shutdown or a governed
        # generation replacement, never a crash worth recording.
        try:
            from core_system.tool_isolation import get_isolation_manager
            get_isolation_manager().stop_monitor()
        except Exception:
            pass

        # NOTE: Do NOT force-close independent/standalone tools on main system shutdown.
        # Each standalone tool runs in its own process and manages its own lifecycle.
        # The main system only stops its own internal services and sovereigns.
        # Independent tools with has_custom_ui and main_system_independent_tool=true
        # are NOT force-closed here; they continue running in their own processes.

        # Stop sub-sovereigns
        for sov in self._sub_sovereigns.values():  # type: ignore[attr-defined]
            try:
                await sov.stop()
            except Exception:
                pass

        # Stop sovereigns (A63/A64: decision only, execution delegated).
        # Each stop is isolated so one failure cannot skip the rest —
        # a single faulty sovereign must never leak the remaining
        # children/tasks through an aborted shutdown sequence.
        # Stop the system automation coordinator first so it does not
        # route degradation signals while sovereigns are shutting down.
        try:
            await self.system_automation_coordinator.stop()  # type: ignore[attr-defined]
        except Exception:
            pass
        # Stop the on-demand model activation broker before the toolbox
        # service goes away so no activation races the shutdown.
        try:
            broker = getattr(self, "model_service_activation", None)
            if broker is not None:
                await broker.stop()
        except Exception:
            pass
        try:
            sleeper = getattr(self, "sleep_policy", None)
            if sleeper is not None:
                await sleeper.stop()
        except Exception:
            pass
        # Release the saga runtime assembly (no background thread).
        try:
            saga_runtime = getattr(self, "saga_runtime", None)
            if saga_runtime is not None:
                saga_runtime.stop()
        except Exception:
            pass
        for _sovereign in (
            self.automation_sovereign,  # type: ignore[attr-defined]
            self.xingcheng_sovereign,  # type: ignore[attr-defined]
            self.system_runtime_sovereign,  # type: ignore[attr-defined]
            self.permission_sovereign,  # type: ignore[attr-defined]
            self.decision_sovereign,  # type: ignore[attr-defined]
        ):
            try:
                await _sovereign.stop_supervision()
            except Exception:
                pass
            try:
                await _sovereign.stop()
            except Exception:
                pass

        for _service in (
            self.daily_global_cleaner_service,  # type: ignore[attr-defined]
            self.hot_update_service,  # type: ignore[attr-defined]
            self.update_manager,  # type: ignore[attr-defined]
        ):
            if _service is None:
                continue
            try:
                await _service.stop()
            except Exception:
                pass

        # Stop maintenance controller (Database Auto Maintenance v1)
        try:
            maintenance_integration = getattr(self, "maintenance_controller_integration", None)
            if maintenance_integration is not None:
                await maintenance_integration.stop()
        except Exception:
            pass

        # Stop CAG integration (DAG+CAG+RAG hybrid architecture)
        try:
            cag_integration = getattr(self, "cag_integration", None)
            if cag_integration is not None:
                await cag_integration.stop()
        except Exception:
            pass

        # Stop the canonical RAG runtime after its CAG consumer
        try:
            rag_runtime = getattr(self, "rag_runtime", None)
            if rag_runtime is not None:
                await rag_runtime.stop()
        except Exception:
            pass

        watcher = self.hot_reload_watcher  # type: ignore[attr-defined]
        if watcher is not None:
            await watcher.stop()
        reanchor = self.authority_reanchor_service  # type: ignore[attr-defined]
        if reanchor is not None:
            reanchor.stop()

        # Window-backed tools are closed above before the sovereign stack is
        # stopped, so no UI-owned backend remains after application exit.

        pending_tasks = [task for task in self._command_tasks if not task.done()]  # type: ignore[attr-defined]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            _done, still_running = await asyncio.wait(
                pending_tasks,
                timeout=10,
            )
            if still_running:
                self._log(  # type: ignore[attr-defined]
                    {
                        "type": "warning",
                        "message": (
                            f"{len(still_running)} command task(s) retained "
                            "durable recovery state after shutdown deadline"
                        ),
                    }
                )

        self._command_tasks.clear()  # type: ignore[attr-defined]
        self._command_task_meta.clear()  # type: ignore[attr-defined]

        # §10.63 R3: stop the shared periodic loop last — all consumers
        # (coordinator/cleaner/memory maintainer) have unregistered above.
        periodic_scheduler = getattr(self, "periodic_scheduler", None)
        if periodic_scheduler is not None:
            try:
                await periodic_scheduler.stop()
            except Exception:
                pass

        await self.runtime_bootstrap.shutdown()  # type: ignore[attr-defined]
        if self.governance is not None:  # type: ignore[attr-defined]
            self.governance.close()  # type: ignore[attr-defined]
            self.governance = None  # type: ignore[attr-defined]
