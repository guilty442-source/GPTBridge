"""IPC server handler helpers (A185 split).

Contains the heartbeat monitor and cleanup helpers extracted from handler.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timezone
from typing import Any

import websockets  # type: ignore

from core.ui_shell import UIShell
from tasks.state_change_notifier import StateChangeNotifier
from tasks.state_outbox import OutboxPublisher
from tasks.connection_watchdog import write_ipc_connection_state


async def _run_heartbeat_monitor(
    ui: UIShell,
    websocket: Any,
    app_instance: Any,
    heartbeat_dead: asyncio.Event,
    heartbeat_state: dict[str, Any],
) -> None:
    """Run the heartbeat monitor loop.

    Sends periodic heartbeat pings and closes the connection if the client
    does not respond within the timeout.
    """
    HEARTBEAT_INTERVAL = 5.0
    HEARTBEAT_TIMEOUT = 20.0
    while not heartbeat_dead.is_set():
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        if heartbeat_dead.is_set():
            break
        try:
            # Bound the send: a backpressured or half-dead socket would
            # otherwise stall this monitor forever without ever marking
            # the connection dead.  A stalled send is itself a dead
            # connection signal.
            await asyncio.wait_for(
                ui.send_event(
                    "heartbeat_ping",
                    {"t": datetime.now(timezone.utc).isoformat()},
                ),
                timeout=HEARTBEAT_INTERVAL,
            )
        except Exception:
            heartbeat_dead.set()
            break
        if (
            heartbeat_state.get("read_loop_active", False)
            and time.monotonic() - heartbeat_state.get("last_pong", 0.0)
            > HEARTBEAT_TIMEOUT
        ):
            # Client has not responded in 20s — close dead connection
            heartbeat_dead.set()
            try:
                app_instance._log(
                    {"type": "ipc_heartbeat_timeout", "reason": "pong_timeout"}
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    websocket.close(code=1001, reason="heartbeat_timeout"),
                    timeout=5.0,
                )
            except Exception:
                pass
            break


async def _cleanup_connection(
    app_instance: Any,
    ui: UIShell,
    heartbeat_dead: asyncio.Event,
    heartbeat_task: asyncio.Task,
    connection_tasks: set,
    project_root: Any,
) -> None:
    """Clean up the connection after the read loop exits."""
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
        write_ipc_connection_state(project_root, _active)
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


__all__ = ["_run_heartbeat_monitor", "_cleanup_connection"]
