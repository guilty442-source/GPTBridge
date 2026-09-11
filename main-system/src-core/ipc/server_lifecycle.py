"""IPC server lifecycle: noise filter, HTTP response helper, and ``run_server``.

Extracted from ``ipc.server`` to keep each module focused and under 500 lines.
All names here are re-exported by ``ipc.server`` for backward compatibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import websockets  # type: ignore

if TYPE_CHECKING:
    from main import GPTBridgeApp

from core_system.resource_maintenance import IdleMemoryMaintainer
from .server_tokens import (
    _ipc_port,
    _shutdown_request_authorized,
    _shutdown_request_is_manual,
    _websocket_request_authorized,
    _workspace_instance_id,
)
from .server_process import (
    _get_port_owner,
    _is_gptbridge_process,
    _kill_process,
)
from .server_handler import handler, _runtime_status_push_loop
from tasks.state_change_notifier import StateChangeNotifier
from tasks.state_outbox import OutboxPublisher


# ------------------------------------------------------------------
# Logging noise filter
# ------------------------------------------------------------------

class _ExpectedProbeNoiseFilter(logging.Filter):
    """Hide expected health/TCP probe disconnects without hiding real errors."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.getMessage() != "opening handshake failed":
            return True
        exception = record.exc_info[1] if record.exc_info else None
        return not isinstance(
            exception,
            (
                websockets.exceptions.ConnectionClosedError,
                websockets.exceptions.InvalidMessage,
            ),
        )


# ------------------------------------------------------------------
# HTTP response helper (with fallback for older websockets)
# ------------------------------------------------------------------

# Safe fallback for older websockets versions to prevent ImportError crashes
try:
    from websockets.http11 import Response
    from websockets.datastructures import Headers
    def http_response(status_code: int, reason: str, body: bytes, content_type: str = "text/plain") -> Any:
        return Response(
            status_code,
            reason,
            Headers(
                [
                    ("Content-Type", content_type),
                    ("Content-Length", str(len(body))),
                ]
            ),
            body,
        )
except ImportError:
    import http
    def http_response(status_code: int, reason: str, body: bytes, content_type: str = "text/plain") -> Any:
        status = http.HTTPStatus(status_code)
        return (status, [("Content-Type", content_type), ("Content-Length", str(len(body)))], body)


# ------------------------------------------------------------------
# Trusted origins
# ------------------------------------------------------------------

TRUSTED_WEBSOCKET_ORIGINS = (
    None,
    "file://",
    "null",
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:5180",
    "http://localhost:5180",
    "http://127.0.0.1:5183",
    "http://localhost:5183",
)


# ------------------------------------------------------------------
# Server entry point
# ------------------------------------------------------------------

async def run_server(app_instance, auto_kill_backend_port: bool = False):
    ipc_port = _ipc_port()
    memory_task: asyncio.Task[Any] | None = None
    memory_maintainer: IdleMemoryMaintainer | None = None
    try:
        async def bound_handler(ws):
            await handler(ws, app_instance)

        shutdown_event = asyncio.Event()
        memory_maintainer = IdleMemoryMaintainer(
            is_busy=lambda: bool(getattr(app_instance, "_command_tasks", set())),
        )

        def process_request_with_shutdown(_connection, request):
            parsed_request = urlsplit(str(request.path))
            request_path = parsed_request.path
            if request_path == "/health":
                startup_status = (
                    app_instance.get_startup_status()
                    if hasattr(app_instance, "get_startup_status")
                    else {}
                )
                # A67 four-condition readiness gate:
                #   backend-runtime-ready + governance-ready
                #   + required-dependencies-ready + authenticated-ipc-connected
                # A socket being open alone is NOT ready.
                from tasks.readiness_gate import ReadinessGate

                readiness = ReadinessGate(app_instance).evaluate()
                ready = readiness.overall_ready
                runtime_state = readiness.runtime_state
                payload = {
                        "ok": ready,
                        "version": str(getattr(app_instance, "version", "0.0.0")),
                        "workspace_instance_id": _workspace_instance_id(),
                        "runtime_state": runtime_state,
                        "runtime_scope": "main",
                        "governance_ready": readiness.governance_ready,
                        "backend_runtime_ready": readiness.backend_runtime_ready,
                        "dependencies_ready": readiness.dependencies_ready,
                        "authenticated_ipc_connected": readiness.authenticated_ipc_connected,
                        "dependencies": [d.as_dict() for d in readiness.dependencies],
                        "services": {},
                        "capabilities": {},
                    }
                if parsed_request.query != "brief=1":
                    payload.update(
                        memory_maintenance=memory_maintainer.status(),
                        **startup_status,
                    )
                body = json.dumps(
                    payload,
                    ensure_ascii=False,
                ).encode("utf-8")
                if ready:
                    return http_response(200, "OK", body, "application/json")
                return http_response(503, "STARTING", body, "application/json")
            if request_path == "/shutdown":
                if not _shutdown_request_authorized(request):
                    return http_response(403, "FORBIDDEN", b"Forbidden")
                app_instance._manual_shutdown = _shutdown_request_is_manual(request)
                shutdown_event.set()
                return http_response(200, "OK", b"OK")
            if not _websocket_request_authorized(request):
                return http_response(403, "FORBIDDEN", b"Forbidden")
            return None

        # Start the IPC Server first so health checks pass immediately, preventing UI timeouts
        try:
            websocket_logger = logging.getLogger("gptbridge.websockets.server")
            if not any(
                isinstance(item, _ExpectedProbeNoiseFilter)
                for item in websocket_logger.filters
            ):
                websocket_logger.addFilter(_ExpectedProbeNoiseFilter())
            async with websockets.serve(
                bound_handler,
                "127.0.0.1",
                ipc_port,
                origins=TRUSTED_WEBSOCKET_ORIGINS,
                process_request=process_request_with_shutdown,
                logger=websocket_logger,
                ping_interval=None,
                ping_timeout=None,
            ):
                print(f"IPC Server running at ws://127.0.0.1:{ipc_port}")
                if hasattr(app_instance, "_mark_startup_phase"):
                    app_instance._mark_startup_phase("server_listener_ready")

                # A67: initialize the state change notifier for immediate
                # event propagation on readiness transitions.
                app_instance._state_change_notifier = StateChangeNotifier(app_instance)
                # A195: initialize the transactional outbox publisher so
                # backend state changes reach the frontend through a durable,
                # client-acknowledged event stream instead of best-effort push.
                app_instance._outbox_publisher = OutboxPublisher(app_instance)

                try:
                    if hasattr(app_instance, "_mark_startup_phase"):
                        app_instance._mark_startup_phase("runtime_initializing")
                    await app_instance.initialize()
                except Exception as exc:
                    # Single-fault rule: a failed initialization must not take
                    # the server down. The listener stays up in degraded mode —
                    # /health reports runtime_state/degraded and websocket
                    # clients stay connected (commands still fail closed).
                    if hasattr(app_instance, "_mark_startup_phase"):
                        app_instance._mark_startup_phase("runtime_failed")
                    try:
                        app_instance.startup_dead = True
                        app_instance._record_startup_failure("initialize", exc)
                    except Exception:
                        pass
                    try:
                        app_instance._log({"type": "error", "message": f"runtime initialization failed: {exc}"})
                    except Exception:
                        print(f"Runtime initialization failed: {exc}")
                # A67: after initialize(), push immediately so the UI sees the
                # readiness transition without waiting for the 2s timer.
                notifier = getattr(app_instance, "_state_change_notifier", None)
                if notifier is not None:
                    try:
                        await notifier.maybe_notify()
                    except Exception:
                        pass
                memory_task = asyncio.create_task(
                    memory_maintainer.run(shutdown_event),
                    name="main-system-idle-memory-maintenance",
                )
                status_push_task = asyncio.create_task(
                    _runtime_status_push_loop(app_instance, shutdown_event),
                    name="main-system-runtime-status-push",
                )
                outbox_publisher = getattr(app_instance, "_outbox_publisher", None)
                outbox_task = (
                    asyncio.create_task(
                        outbox_publisher.run(shutdown_event),
                        name="main-system-state-outbox",
                    )
                    if isinstance(outbox_publisher, OutboxPublisher)
                    else None
                )

                await shutdown_event.wait()
                status_push_task.cancel()
                if outbox_task is not None:
                    outbox_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await outbox_task
                with contextlib.suppress(asyncio.CancelledError):
                    await status_push_task
        except OSError as exc:
            if exc.errno in {98, 10048}:
                print(
                    f"[IPC] Failed to bind backend server to "
                    f"127.0.0.1:{ipc_port}: address already in use."
                )
                pid, owner = _get_port_owner(ipc_port)
                if owner:
                    print(f"[IPC] Port owner: {owner}")
                if auto_kill_backend_port and pid is not None:
                    if _is_gptbridge_process(pid, Path(__file__).resolve().parents[2]):
                        print(f"[IPC] Detected existing GPTBridge backend process PID {pid}; attempting safe termination.")
                        if _kill_process(pid):
                            print("[IPC] Previous GPTBridge backend terminated. Retrying server bind...")
                            await asyncio.sleep(1)
                            return await run_server(app_instance, auto_kill_backend_port=False)
                        print("[IPC] Failed to terminate the existing GPTBridge backend process.")
                    else:
                        print("[IPC] Existing process does not appear to be a GPTBridge backend; auto-kill aborted.")
                print(
                    f"[IPC] Please stop the existing process on port {ipc_port} "
                    "before starting."
                )
                return
            raise
    except KeyboardInterrupt:
        print("Stopping IPC server...")
    finally:
        if memory_maintainer is not None:
            await memory_maintainer.stop(memory_task)
        await app_instance.shutdown()
