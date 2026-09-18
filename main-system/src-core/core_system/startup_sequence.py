"""Startup Sequence Execution.

Main application startup sequence including sovereign stack, integrations,
services, and runtime readiness.
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Any

from core_system.startup_executor import StartupSovereignExecutor


async def run_startup_sequence(app: Any) -> bool:
    """Execute the complete startup sequence.

    Returns True if startup succeeded, False otherwise.
    """

    if os.environ.get("GPTBRIDGE_LOOP_STALL_DEBUG"):
        from core_system.diagnostics import start_loop_stall_watchdog
        start_loop_stall_watchdog(app)

    # A40/E26: the launcher attestation is consumed at first valid load —
    # the governance authority bootstrap precedes the certified phase DAG.
    if app.governance is None:
        from core_system.governance_runtime import MainSystemGovernance
        app.governance = MainSystemGovernance.from_environment(app.project_root)

    # Check if boot_core has already completed phases 0-5
    startup_state = os.environ.get("GPTBRIDGE_STARTUP_STATE", "")
    generation_id = os.environ.get("GPTBRIDGE_STARTUP_GENERATION", "")

    # A128/A130: Sovereign stack startup sequence (parallel where possible)
    # 1. Peer sovereigns (learning, programming, cleaner) — parallel
    # 2. Permission sovereign (read-only)
    # 3. Maintenance sovereign + self-maintenance — parallel
    # 4. System sovereign + 6 sub-sovereigns — parallel

    app._mark_startup_phase("sovereign_stack_starting")

    # Start the decision-layer sovereigns in parallel
    # Note: decision_sovereign must start last as it orchestrates the stack
    sovereign_tasks = [
        app.permission_sovereign.start(),
        app.system_runtime_sovereign.start(),
        app.automation_sovereign.start(),
        app.xingcheng_sovereign.start(),
    ]
    try:
        await asyncio.gather(*sovereign_tasks, return_exceptions=True)
    except Exception as error:
        app._record_startup_failure("sovereign_stack_start", error)
    app._mark_startup_phase("top_sovereigns_started")

    # Start decision sovereign last (orchestrates the stack)
    try:
        await app.decision_sovereign.start()
    except Exception as error:
        app._record_startup_failure("decision_sovereign", error)
    app._mark_startup_phase("decision_sovereign_started")

    # Start supervision/automation loops separately (A297 separation:
    # sovereigns decide; the governed executor starts observation work).
    _sup_timings: dict[str, int] = {}

    async def _timed_supervision(name: str, coro: Any) -> None:
        _t0 = time.monotonic()
        try:
            await coro
        finally:
            _sup_timings[name] = int((time.monotonic() - _t0) * 1000)

    supervision_tasks = [
        _timed_supervision(
            "permission", app.permission_sovereign.start_supervision()
        ),
        _timed_supervision(
            "system-runtime",
            app.system_runtime_sovereign.start_supervision(),
        ),
        _timed_supervision(
            "automation", app.automation_sovereign.start_supervision()
        ),
        _timed_supervision(
            "xingcheng", app.xingcheng_sovereign.start_supervision()
        ),
    ]
    try:
        await asyncio.gather(*supervision_tasks, return_exceptions=True)
    except Exception as error:
        app._record_startup_failure("sovereign_supervision_start", error)
    app._mark_startup_phase("sovereign_supervision_started")

    try:
        await _timed_supervision(
            "decision", app.decision_sovereign.start_supervision()
        )
    except Exception as error:
        app._record_startup_failure("decision_sovereign_supervision", error)
    app._mark_startup_phase("decision_supervision_started")

    # Start the system-wide automation coordinator after all
    # sovereigns are started.  The coordinator aggregates health
    # and routes cross-sovereign degradation; it does not start
    # individual sovereign loops (those start via _on_start).
    try:
        await _timed_supervision(
            "coordinator", app.system_automation_coordinator.start()
        )
    except Exception as error:
        app._record_startup_failure(
            "system_automation_coordinator", error
        )
    app._log({"type": "supervision_start_timings", **_sup_timings})
    app._mark_startup_phase("automation_coordinator_started")

    # Start the maintenance controller (Database Auto Maintenance v1)
    # Starts after governance validated, security validated, database foundation ready
    try:
        result = await app.maintenance_controller_integration.start()
        if result.get("ok"):
            app.maintenance_ready = True
            app._log({"type": "status", "message": "Maintenance controller started", **result})
        else:
            app._record_startup_failure("maintenance_controller", RuntimeError(result.get("reason", "unknown")))
    except Exception as error:
        app._record_startup_failure("maintenance_controller", error)
    app._mark_startup_phase("maintenance_controller_started")

    # Start the canonical RAG runtime (DAG+CAG+RAG hybrid architecture)
    # Must precede CAG: cag_integration reads app.rag_orchestrator.
    try:
        result = await app.rag_runtime.start()
        if result.get("ok"):
            app.rag_ready = True
            app._log({"type": "status", "message": "RAG runtime started", **result})
        else:
            app._record_startup_failure("rag_runtime", RuntimeError(result.get("reason", "unknown")))
    except Exception as error:
        app._record_startup_failure("rag_runtime", error)
    app._mark_startup_phase("rag_runtime_started")

    # Start CAG context preloading (DAG+CAG+RAG hybrid architecture)
    # Starts after RAG orchestrator is available
    try:
        result = await app.cag_integration.start()
        if result.get("ok"):
            app.cag_ready = True
            app._log({"type": "status", "message": "CAG context preloading started", **result})
        else:
            app._record_startup_failure("cag_integration", RuntimeError(result.get("reason", "unknown")))
    except Exception as error:
        app._record_startup_failure("cag_integration", error)
    app._mark_startup_phase("cag_integration_started")

    # Check if boot_core has already completed phases 0-5
    startup_state = os.environ.get("GPTBRIDGE_STARTUP_STATE", "")

    if startup_state in ("READY", "DEGRADED"):
        # CAPABILITY 1 already complete — run CAPABILITY 2 only
        app._mark_startup_phase("capability-2-startup-executor")
        from core_system.startup_executor import StartupSovereignExecutor

        executor = StartupSovereignExecutor(app)
        # Inject the generation ID from boot_core for continuity
        result = await executor.run(generation_id=os.environ.get("GPTBRIDGE_STARTUP_GENERATION", ""))
        startup_ok = result.ok
        if not startup_ok:
            app._record_startup_failure(
                result.failure_phase or "startup-generation",
                RuntimeError(
                    ";".join(result.violations)
                    or next(
                        (p.error for p in result.phases if p.error),
                        "startup-generation-failed",
                    )
                ),
            )
            # E155 PARTIAL-READY:none — a failed generation must not start
            # post-handoff runtime duties (watchers, hot-update, isolation
            # monitor).  The listener stays up in degraded mode via
            # run_server; the executor already ran reverse cleanup and set
            # startup_dead.
            return False
    else:
        # Standalone mode — run full startup sequence (CAPABILITY 1 + 2)
        app._mark_startup_phase("full-startup-sequence")
        from core_system.startup_executor import StartupSovereignExecutor

        executor = StartupSovereignExecutor(app)
        result = await executor.run()
        startup_ok = result.ok
        if not startup_ok:
            app._record_startup_failure(
                result.failure_phase or "startup-generation",
                RuntimeError(
                    ";".join(result.violations)
                    or next(
                        (p.error for p in result.phases if p.error),
                        "startup-generation-failed",
                    )
                ),
            )
            return False

    # Automated hot-reload watcher — requests a governed, module-scoped
    # reload through the maintenance sovereign when backend source changes
    # quiet down.  Observation is separate from decision/execution.
    app._mark_startup_phase("hot_reload_watcher_starting")
    try:
        from tasks.hot_reload_watcher import HotReloadWatcher

        app.hot_reload_watcher = HotReloadWatcher(app)
        await app.hot_reload_watcher.start()
    except Exception as error:
        app._record_startup_failure("hot_reload_watcher", error)
    app._mark_startup_phase("hot_reload_watcher_started")

    # Governed authority re-anchor — adopts codex / managed-registry
    # updates in-process so authority changes never require a restart.
    app._mark_startup_phase("authority_reanchor_starting")
    try:
        from core_system.authority_reanchor_service import (
            AuthorityReanchorService,
        )

        app.authority_reanchor_service = AuthorityReanchorService(app)
        app.authority_reanchor_service.start()
    except Exception as error:
        app._record_startup_failure("authority_reanchor", error)
    app._mark_startup_phase("authority_reanchor_started")

    # Start the hot-update idle loop so deferred resource-holding module
    # replacements are applied automatically when the system is idle.
    try:
        await app.hot_update_service.start()
    except Exception as error:
        app._record_startup_failure("hot_update_service", error)

    # Initialize UpdateManager for enhanced self-update capability
    try:
        from core_system.update_manager import UpdateManager
        app.update_manager = UpdateManager(app, app.hot_update_service, app.project_root)
        await app.update_manager.start_auto_update()
        app._log({"type": "status", "message": "UpdateManager started"})
    except Exception as error:
        app._record_startup_failure("update_manager", error)

    # Start the tool isolation health monitor and wire crash events to
    # the state change notifier so the UI sees tool crashes immediately.
    try:
        from core_system.tool_isolation import get_isolation_manager
        iso_mgr = get_isolation_manager(app.project_root)
        notifier = getattr(app, "_state_change_notifier", None)
        loop = asyncio.get_event_loop()
        if notifier is not None:
            def _on_crash(tool_id: str, entry: Any) -> None:
                crash_info = {
                    "pid": getattr(entry, "pid", None),
                    "restart_count": getattr(entry, "restart_count", 0),
                    "exit_code": getattr(entry, "process", None),
                }
                if hasattr(entry, "process") and entry.process is not None:
                    crash_info["exit_code"] = entry.process.returncode
                notifier.push_tool_crash_event(tool_id, crash_info, loop=loop)
            iso_mgr.register_crash_callback(_on_crash)
        iso_mgr.start_monitor(interval=30.0, light=True)
    except Exception as error:
        app._record_startup_failure("tool_isolation_monitor", error)
    app._mark_startup_phase("main_runtime_ready")
    app._log({
        "type": "status",
        "status": "ready" if startup_ok else "maintenance_incomplete",
        "maintenance_ready": startup_ok,
    })

    return startup_ok


__all__ = ["run_startup_sequence"]