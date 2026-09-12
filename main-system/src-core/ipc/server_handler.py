"""WebSocket connection handler and runtime-status push loop.

Extracted from ``ipc.server`` to keep each module focused and under 500 lines.
All names here are re-exported by ``ipc.server`` for backward compatibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import websockets  # type: ignore

if TYPE_CHECKING:
    from main import GPTBridgeApp

from core.ui_shell import UIShell
from tasks.connection_watchdog import write_ipc_connection_state
from tasks.state_change_notifier import StateChangeNotifier
from tasks.state_outbox import OutboxPublisher
from .server_commands import process_command_task


MAX_CONNECTION_COMMAND_TASKS = 32


async def handler(websocket, app_instance):
    ui = UIShell(websocket)
    connection_tasks: set[asyncio.Task] = set()

    # Track active WebSocket connections for the connection watchdog.
    # write_ipc_connection_state expects the workspace root and appends
    # main-system/runtime/state itself.
    _PROJECT_ROOT = Path(__file__).resolve().parents[3]
    try:
        _active_connections = getattr(app_instance, "_active_ws_connections", 0) + 1
        app_instance._active_ws_connections = _active_connections
        write_ipc_connection_state(_PROJECT_ROOT, _active_connections)
    except Exception:
        pass

    # A67 condition 4: authenticated-ipc-connected — tracked as an
    # INDEPENDENT verification channel, not inferred from the session
    # token.  Reaching ``handler`` means the WebSocket handshake passed
    # ``_websocket_request_authorized`` (token + instance id HMAC check)
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
    read_loop_active = False

    async def _heartbeat_monitor() -> None:
        HEARTBEAT_INTERVAL = 5.0
        HEARTBEAT_TIMEOUT = 20.0
        while not heartbeat_dead.is_set():
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            if heartbeat_dead.is_set():
                break
            try:
                await ui.send_event("heartbeat_ping", {"t": datetime.now(timezone.utc).isoformat()})
            except Exception:
                heartbeat_dead.set()
                break
            if (
                read_loop_active
                and time.monotonic() - last_pong_time > HEARTBEAT_TIMEOUT
            ):
                # Client has not responded in 20s — close dead connection
                heartbeat_dead.set()
                try:
                    await websocket.close(code=1001, reason="heartbeat_timeout")
                except Exception:
                    pass
                break

    # Track pong responses via a command handler
    last_pong_time = time.monotonic()
    heartbeat_task = asyncio.create_task(_heartbeat_monitor())

    # Gracefully wait for the backend to finish its heavy initialization.
    # If startup failed outright (startup_dead), do not stall the connection:
    # enter degraded mode so the client stays connected and can observe
    # status; commands still fail closed per-command.
    while (
        app_instance.command_router is None
        and not getattr(app_instance, "startup_dead", False)
    ):
        await asyncio.sleep(0.5)

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

    read_loop_active = True
    try:
        async for message in websocket:
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
                    last_pong_time = time.monotonic()
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
                await ui.send_error(str(exc))
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        heartbeat_dead.set()
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        for task in list(connection_tasks):
            if not task.done():
                task.cancel()
        if connection_tasks:
            await asyncio.gather(*connection_tasks, return_exceptions=True)
        # Decrement active WebSocket connections for the connection watchdog.
        try:
            _active = max(0, getattr(app_instance, "_active_ws_connections", 1) - 1)
            app_instance._active_ws_connections = _active
            write_ipc_connection_state(_PROJECT_ROOT, _active)
        except Exception:
            pass
        # A67 condition 4: decrement the independent authenticated-IPC counter.
        try:
            _authed = max(0, getattr(app_instance, "_authenticated_ipc_connections", 1) - 1)
            app_instance._authenticated_ipc_connections = _authed
        except Exception:
            pass
        # Remove this UIShell from the real-time push set.
        try:
            app_instance._active_ui_shells.discard(ui)
        except Exception:
            pass
        # A195: unregister the outbox session for this connection.
        try:
            publisher = getattr(app_instance, "_outbox_publisher", None)
            if isinstance(publisher, OutboxPublisher):
                publisher.unregister_session(ui)
        except Exception:
            pass
        # A67: authenticated IPC connection count changed — push immediately.
        notifier = getattr(app_instance, "_state_change_notifier", None)
        if isinstance(notifier, StateChangeNotifier):
            try:
                asyncio.create_task(notifier.maybe_notify())
            except Exception:
                pass


async def _runtime_status_push_loop(app_instance, shutdown_event: asyncio.Event) -> None:
    """Periodically push runtime status to all connected WebSocket clients.

    This replaces the frontend's 5-second polling with real-time server push.
    The push interval is 2 seconds — fast enough for responsive UI updates
    without overwhelming the WebSocket channel.
    """
    push_interval = 2.0
    _PROJECT_ROOT = Path(__file__).resolve().parents[3]
    while not shutdown_event.is_set():
        try:
            # Keep ipc-connections.json fresh for the connection watchdog.
            # The watchdog rejects a state file older than 20s, while the
            # open/close hooks only write on transitions, so a stable healthy
            # connection would otherwise be misread as a frontend disconnect.
            _connection_count = getattr(app_instance, "_active_ws_connections", 0)
            write_ipc_connection_state(_PROJECT_ROOT, _connection_count)

            shells = getattr(app_instance, "_active_ui_shells", None)
            if shells:
                notifier = getattr(app_instance, "_state_change_notifier", None)
                snapshot = (
                    notifier.current_snapshot()
                    if isinstance(notifier, StateChangeNotifier)
                    else None
                )
                if snapshot is not None:
                    status_payload = snapshot.as_dict()
                    status_payload["systemReady"] = snapshot.overall_ready
                    status_payload["push"] = True
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
        await asyncio.sleep(push_interval)
