"""WebSocket connection handler and runtime-status push loop.

Extracted from ``ipc.server`` to keep each module focused and under 500 lines.
All names here are re-exported by ``ipc.server`` for backward compatibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import websockets  # type: ignore

from core.ui_shell import UIShell
from tasks.connection_watchdog import write_ipc_connection_state
from tasks.state_change_notifier import StateChangeNotifier
from tasks.state_outbox import OutboxPublisher
from .server_commands import process_command_task
from .server_handler_helpers import _run_heartbeat_monitor, _cleanup_connection
from shared_layer.observability.tracing import (
    CorrelationContext,
    set_correlation_context,
    set_correlation_id,
)


MAX_CONNECTION_COMMAND_TASKS = 32


async def handler(websocket, app_instance):
    ui = UIShell(websocket)
    connection_tasks: set[asyncio.Task] = set()

    # Track active WebSocket connections for the connection watchdog.
    # write_ipc_connection_state expects the workspace root and appends
    # main-system/runtime/state itself.
    _PROJECT_ROOT = Path(__file__).resolve().parents[3]
    # §10.65 act-1: native transport shadow — one per connection; policy
    # mode != "shadow" or a missing extension yields None (fail-closed).
    try:
        from .ipc_transport_native_shadow import IpcTransportNativeShadow

        _ipc_shadow = IpcTransportNativeShadow.from_policy(_PROJECT_ROOT)
    except Exception:
        _ipc_shadow = None
    try:
        _active_connections = getattr(app_instance, "_active_ws_connections", 0) + 1
        app_instance._active_ws_connections = _active_connections
        write_ipc_connection_state(_PROJECT_ROOT, _active_connections)
    except Exception:
        pass

    # A67 condition 4: authenticated-ipc-connected — tracked as an
    # INDEPENDENT verification channel, not inferred from the session
    # token.  Reaching ``handler`` means the WebSocket handshake passed
    # ``_websocket_request_authorized`` (short-lived ticket or governed legacy token + instance check)
    # in ``process_request``; an unauthenticated connection is rejected
    # with 403 before it ever gets here.  We therefore count this as an
    # explicitly authenticated IPC connection, distinct from the raw
    # ``_active_ws_connections`` counter (which only reflects an open
    # socket).  The readiness gate consults this independent counter so
    # readiness cannot be satisfied by a socket that bypassed auth.
    try:
        _authed = getattr(app_instance, "_authenticated_ipc_connections", 0) + 1
        app_instance._authenticated_ipc_connections = _authed
    except Exception:
        pass

    # Register this UIShell so the status push loop can send real-time updates.
    if not hasattr(app_instance, "_active_ui_shells"):
        app_instance._active_ui_shells: set[UIShell] = set()
    app_instance._active_ui_shells.add(ui)

    # A195: register this authenticated session with the transactional
    # outbox publisher; events begin flowing after the client's hello.
    publisher = getattr(app_instance, "_outbox_publisher", None)
    if isinstance(publisher, OutboxPublisher):
        publisher.register_session(ui)

    # A67: authenticated IPC connection count changed — push immediately so
    # the UI reflects the new readiness state without waiting for the 2s timer.
    notifier = getattr(app_instance, "_state_change_notifier", None)
    if isinstance(notifier, StateChangeNotifier):
        asyncio.create_task(notifier.maybe_notify())

    # The backend owns the refresh: send this client an immediate compact
    # health report instead of waiting for the next push cycle.  The client
    # only renders reports it receives; it never polls.
    try:
        status_service = getattr(app_instance, "runtime_status_service", None)
        compact = getattr(status_service, "compact_status", None)
        if callable(compact):
            # Off the event loop: the readiness projection must never block
            # the IPC channel, even if a dependency probe is slow.
            report = await asyncio.to_thread(compact)
            report["push"] = True
            report["immediate"] = True
            await ui.send_event("runtime_status_push", report)
    except Exception:
        pass

    # Start heartbeat monitor BEFORE the startup wait — keeps the connection
    # warm while the backend finishes heavy initialization, so the client's
    # stale-connection detector does not kill a healthy socket.  The
    # frontend responds to "heartbeat_ping" with "heartbeat_pong"; once the
    # read loop below starts consuming messages, a client that stays silent
    # for HEARTBEAT_TIMEOUT_SECONDS is considered dead and closed.
    # Library-level ping/pong is disabled (ping_interval=None in serve());
    # this application-level heartbeat is the sole connection health check.
    heartbeat_dead = asyncio.Event()
    # Pongs arriving during the startup wait sit in the socket buffer until
    # the read loop starts — enforce the pong timeout only from that point.
    # The monitor runs concurrently with this coroutine, so the liveness
    # clock is shared through a mutable holder rather than copied by value.
    heartbeat_state = {
        "last_pong": time.monotonic(),
        "read_loop_active": False,
    }

    heartbeat_task = asyncio.create_task(
        _run_heartbeat_monitor(
            ui, websocket, app_instance, heartbeat_dead, heartbeat_state,
        )
    )

    # Gracefully wait for the backend to finish its heavy initialization.
    # If startup failed outright (startup_dead), do not stall the connection:
    # enter degraded mode so the client stays connected and can observe
    # status; commands still fail closed per-command.
    while (
        app_instance.command_router is None
        and not getattr(app_instance, "startup_dead", False)
    ):
        await asyncio.sleep(0.1)

    if getattr(app_instance, "startup_dead", False):
        await ui.send_event(
            "runtime_degraded",
            {
                "ok": False,
                "runtime_state": "degraded",
                "startup_failures": list(
                    getattr(app_instance, "startup_failures", [])
                ),
            },
        )

    if getattr(app_instance, "task_queue", None):
        pending = app_instance.task_queue.pending_recovery()
        if pending:
            await ui.send_event("task_recovery_required", {"ok": True, "tasks": pending})

    # Reset the liveness clock when the read loop activates: pongs buffered
    # during the startup wait have not been consumed yet, so a long
    # initialization must not immediately trip the heartbeat timeout.
    heartbeat_state["last_pong"] = time.monotonic()
    heartbeat_state["read_loop_active"] = True
    try:
        async for message in websocket:
            # Any inbound frame proves the client is alive — count every
            # message as liveness, not only heartbeat_pong responses, so a
            # busy session is never killed while traffic is flowing.
            heartbeat_state["last_pong"] = time.monotonic()
            if _ipc_shadow is not None:
                with contextlib.suppress(Exception):
                    _ipc_shadow.observe_inbound(message)
            try:
                data = json.loads(message)
                if not isinstance(data, dict):
                    raise ValueError("IPC message must be a JSON object")
                command = data.get("command")
                payload = data.get("payload") or {}
                if not isinstance(command, str) or not command.strip():
                    raise ValueError("IPC command must be a non-empty string")
                command = command.strip()
                if not isinstance(payload, dict):
                    raise ValueError("IPC payload must be a JSON object")

                # Establish trace context for this command: honour an inbound
                # _trace_context when the caller propagates one, otherwise
                # start a fresh correlation so downstream injection (outbound
                # events, spawned-tool env, otel spans) has real values.
                trace_context = payload.pop("_trace_context", None)
                if trace_context and trace_context.get("correlation_id"):
                    set_correlation_id(trace_context.get("correlation_id", ""))
                    set_correlation_context(trace_context)
                else:
                    fresh = CorrelationContext.new()
                    set_correlation_id(fresh.correlation_id)
                    set_correlation_context(
                        {
                            "correlation_id": fresh.correlation_id,
                            "parent_id": fresh.parent_id,
                            "trace_id": fresh.trace_id,
                            "span_id": fresh.span_id,
                            "baggage": fresh.baggage,
                            "metadata": fresh.metadata,
                        }
                    )

                if command == "task_recovery_decision":
                    resume = bool(payload.get("resume"))
                    result = app_instance.task_queue.resolve_recovery(resume) if getattr(app_instance, "task_queue", None) else {
                        "ok": True,
                        "resume": resume,
                        "task_count": 0,
                    }
                    await ui.send_event("task_recovery_decision_result", result)
                    continue

                if command == "heartbeat_pong":
                    heartbeat_state["last_pong"] = time.monotonic()
                    continue

                # A195 outbox control channel — handled in-band so cursor
                # moves are ordered with respect to event delivery.
                if command == "state_event_hello":
                    pub = getattr(app_instance, "_outbox_publisher", None)
                    if isinstance(pub, OutboxPublisher):
                        hello = pub.handle_hello(ui, payload.get("cursor"))
                        await ui.send_event("state_event_session", hello)
                    continue

                if command == "state_event_ack":
                    pub = getattr(app_instance, "_outbox_publisher", None)
                    if isinstance(pub, OutboxPublisher):
                        pub.handle_ack(ui, payload.get("cursor"))
                    continue

                if command == "state_event_resync":
                    pub = getattr(app_instance, "_outbox_publisher", None)
                    if isinstance(pub, OutboxPublisher):
                        result = pub.handle_resync(ui, payload.get("cursor"))
                        await ui.send_event("state_event_resync_result", result)
                    continue

                if len(connection_tasks) >= MAX_CONNECTION_COMMAND_TASKS:
                    await ui.send_error("Too many commands are already running")
                    continue

                task = asyncio.create_task(process_command_task(app_instance, ui, command, payload))
                connection_tasks.add(task)
                app_instance._command_tasks.add(task)
                app_instance._command_task_meta[task] = {"command": command}

                def clear_command_task(done_task):
                    connection_tasks.discard(done_task)
                    app_instance._command_tasks.discard(done_task)
                    app_instance._command_task_meta.pop(done_task, None)
                    if not done_task.cancelled():
                        with contextlib.suppress(Exception):
                            done_task.exception()

                task.add_done_callback(clear_command_task)
                await ui.send_event("COMMAND_RECEIVED", {"command": command, "status": "processing"})

            except Exception as exc:
                with contextlib.suppress(Exception):
                    await ui.send_error(str(exc))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        await _cleanup_connection(
            app_instance, ui, heartbeat_dead, heartbeat_task,
            connection_tasks, _PROJECT_ROOT,
        )


def _compact_pending_actions(app_instance) -> list[dict[str, Any]]:
    """Per-item pending surface without bulky detail (fault report payload).

    Terminal/reconciled records stay in the durable queue as evidence but
    are not part of the live fault surface.
    """
    try:
        from core_system.auto_action_policy import read_actionable_pending_actions

        project_root = getattr(app_instance, "project_root", None)
        actions = (
            read_actionable_pending_actions(project_root)
            if project_root
            else []
        )
        fields = (
            "action_id",
            "kind",
            "summary",
            "status",
            "fault_id",
            "update_id",
            "scope",
            "target",
            "proposed_method",
            "repair_plan",
            "repair_requirements",
            "risk",
            "rollback",
            "expires_at",
            "evidence_digest",
            "created_at",
            "updated_at",
            "confirmation",
        )
        return [
            {key: action.get(key) for key in fields if key in action}
            for action in actions
            if isinstance(action, dict)
        ]
    except Exception:
        return []


def _maybe_write_connection_state(
    project_root: Path, count: int, state: dict[str, Any]
) -> None:
    # The watchdog rejects a state file older than 20s, so a 10s keepalive
    # (or any count transition) keeps it fresh while cutting the per-cycle
    # atomic write to a fraction of its previous frequency.
    now = time.monotonic()
    if count == state["count"] and now - state["at"] < 10.0:
        return
    state["count"] = count
    state["at"] = now
    write_ipc_connection_state(project_root, count)


async def _runtime_status_push_loop(app_instance, shutdown_event: asyncio.Event) -> None:
    """Backend-owned refresh loop pushing compact health reports to clients.

    Every cycle pushes a compact report; a fault or its resolution wakes the
    loop immediately and adds the per-item pending surface so Xingcheng is
    informed in real time.  All projection work runs off the event loop so a
    slow probe can never stall the IPC channel or the main system.
    """
    push_interval = 2.0
    # §10.63 R3: with no UI shell connected there is nothing to push to —
    # drop to a slow housekeeping cadence (the connect path already sends an
    # immediate report, so a new client never waits for this timer).
    idle_interval = 60.0
    _PROJECT_ROOT = Path(__file__).resolve().parents[3]
    _conn_state = {"count": -1, "at": 0.0}
    while not shutdown_event.is_set():
        had_shells = False
        try:
            _maybe_write_connection_state(
                _PROJECT_ROOT,
                getattr(app_instance, "_active_ws_connections", 0),
                _conn_state,
            )

            fault_event = None
            immediate = False
            try:
                from core_system.auto_action_policy import fault_change_event

                fault_event = fault_change_event()
                if fault_event.is_set():
                    fault_event.clear()
                    immediate = True
            except Exception:
                fault_event = None

            shells = getattr(app_instance, "_active_ui_shells", None)
            if shells:
                had_shells = True
                notifier = getattr(app_instance, "_state_change_notifier", None)
                snapshot = (
                    notifier.current_snapshot()
                    if isinstance(notifier, StateChangeNotifier)
                    else None
                )
                status_service = getattr(app_instance, "runtime_status_service", None)
                compact = getattr(status_service, "compact_status", None)
                status_payload: dict[str, Any] = {}
                if callable(compact):
                    # The backend owns the refresh: every cycle evaluates and
                    # pushes a compact health report; clients only render it.
                    try:
                        status_payload = await asyncio.to_thread(compact, snapshot)
                    except Exception:
                        status_payload = {}
                elif snapshot is not None:
                    status_payload = snapshot.as_dict()
                    status_payload["systemReady"] = snapshot.overall_ready
                    status_payload["maintenance_ready"] = bool(
                        getattr(app_instance, "maintenance_ready", False)
                    )
                if status_payload:
                    if immediate:
                        # Fault or fault-resolution: report immediately with
                        # the per-item surface so Xingcheng needs no refresh.
                        status_payload["pending_actions"] = await asyncio.to_thread(
                            _compact_pending_actions, app_instance
                        )
                        status_payload["fault_report"] = True
                    status_payload["push"] = True
                    status_payload["immediate"] = True
                    dead: list[UIShell] = []
                    for ui in list(shells):
                        try:
                            await ui.send_event("runtime_status_push", status_payload)
                        except Exception:
                            dead.append(ui)
                    for ui in dead:
                        shells.discard(ui)
        except Exception:
            pass
        # Wait for the next cycle, or wake immediately on a fault/clear.
        wait_s = push_interval if had_shells else idle_interval
        try:
            if fault_event is None:
                from core_system.auto_action_policy import fault_change_event

                fault_event = fault_change_event()
            woken = await asyncio.get_running_loop().run_in_executor(
                None, fault_event.wait, wait_s
            )
            if woken:
                fault_event.clear()
        except Exception:
            await asyncio.sleep(wait_s)
