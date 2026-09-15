"""IPC server health check helpers (A185 split).

Contains the health check handler extracted from run_server.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlsplit

from core_system.resource_maintenance import IdleMemoryMaintainer
from .server_tokens import _workspace_instance_id
from .server_lifecycle import http_response


async def _handle_health_request(
    parsed_request: Any,
    app_instance: Any,
    memory_maintainer: IdleMemoryMaintainer,
) -> Any:
    """Handle the /health endpoint request.

    Returns the HTTP response or None if not a health request.
    """
    request_path = parsed_request.path
    if request_path != "/health":
        return None

    # Health check levels (A191/A192 parallel-update safety):
    #   ?level=brief — cached snapshot only, no probes (heartbeat)
    #   ?level=full  — readiness gate + memory + startup_status (default)
    #   ?level=deep  — full + live TCP dependency probes (diagnostics)
    # Legacy ?brief=1 is mapped to ?level=brief for backward compat.
    query = parsed_request.query
    if query == "brief=1" or query == "level=brief":
        health_level = "brief"
    elif query == "level=deep":
        health_level = "deep"
    else:
        health_level = "full"

    def _readiness() -> Any:
        from tasks.readiness_gate import ReadinessGate
        return ReadinessGate(app_instance).evaluate()

    def _startup_status() -> dict[str, Any]:
        if hasattr(app_instance, "get_startup_status"):
            try:
                return app_instance.get_startup_status()
            except Exception:
                pass
        return {}

    def _tool_isolation_status() -> Any:
        try:
            from core_system.tool_isolation import get_isolation_manager
            return get_isolation_manager().status()
        except Exception:
            return None

    # A67 four-condition readiness gate:
    #   backend-runtime-ready + governance-ready
    #   + required-dependencies-ready + authenticated-ipc-connected
    # A socket being open alone is NOT ready.
    notifier = getattr(app_instance, "_state_change_notifier", None)
    readiness = None
    # Brief level: use cached snapshot if available (no probes).
    if health_level == "brief" and notifier is not None:
        readiness = notifier.current_snapshot()
    if readiness is None:
        # Full and deep levels: evaluate the readiness gate off the
        # event loop so TCP dependency probes do not stall the IPC
        # server or heartbeat traffic.  A health endpoint must
        # never block indefinitely — bound the evaluation and
        # fall back to the notifier's last verified snapshot
        # (itself a full gate evaluation) before failing closed.
        try:
            readiness = await asyncio.wait_for(
                asyncio.to_thread(_readiness),
                timeout=5.0,
            )
        except Exception:
            readiness = (
                notifier.current_snapshot()
                if notifier is not None
                else None
            )
            if readiness is None:
                return http_response(
                    503,
                    "STARTING",
                    json.dumps(
                        {
                            "ok": False,
                            "runtime_state": "health-eval-timeout",
                            "runtime_scope": "main",
                            "health_level": health_level,
                        },
                        ensure_ascii=False,
                    ).encode("utf-8"),
                    "application/json",
                )

    startup_status: dict[str, Any] = {}
    iso_status: Any = None
    if health_level != "brief":
        try:
            startup_status, iso_status = await asyncio.wait_for(
                asyncio.gather(
                    asyncio.to_thread(_startup_status),
                    asyncio.to_thread(_tool_isolation_status),
                ),
                timeout=5.0,
            )
        except Exception:
            # Partial report beats a hung health endpoint: keep the
            # gate result truthful and mark the status section as
            # unavailable instead of stalling the response.
            startup_status = {"startup_status_timeout": True}
            iso_status = None

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
        "health_level": health_level,
    }
    # Brief level: skip memory maintenance and startup status
    # to keep the response as fast as possible.
    if health_level != "brief":
        payload.update(
            memory_maintenance=memory_maintainer.status(),
            **startup_status,
        )
        if iso_status is not None:
            payload["tool_isolation"] = iso_status
    body = json.dumps(
        payload,
        ensure_ascii=False,
    ).encode("utf-8")
    if ready:
        return http_response(200, "OK", body, "application/json")
    return http_response(503, "STARTING", body, "application/json")


def _parse_request_path(request: Any) -> Any:
    """Parse the request path from a request."""
    return urlsplit(str(request.path))


__all__ = ["_handle_health_request", "_parse_request_path"]
