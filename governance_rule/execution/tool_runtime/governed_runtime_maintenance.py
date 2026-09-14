"""Maintenance and health mixin for GovernedToolRuntime (A185 split).

Contains self-repair, local cleanup, channel metrics, health
snapshot, and the main run loop with the HTTP process_request handler.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets  # type: ignore

from .sub_sovereign import (
    SUB_SOVEREIGN_AUTHORITY,
    SUB_SOVEREIGN_DUTY,
    SUB_SOVEREIGN_SCOPE,
    SUB_SOVEREIGN_UNDER,
)

from .governed_runtime_constants import http_response as _http_response


class GovernedRuntimeMaintenanceMixin:
    """Maintenance, health, and run loop methods for GovernedToolRuntime."""

    tool_id: str
    tool_root: Any
    sovereign_id: str
    version: str
    token: str
    port: int
    shutdown_token: str
    shutdown_event: asyncio.Event
    executor: Any
    startup_callback: Any
    shutdown_callback: Any
    cancellation: Any
    health_callback: Any
    idle_cleanup: Any
    self_repair_enabled: bool
    self_repair_clear_pycache: bool
    _last_self_repair: Any
    local_cleanup_enabled: bool
    _last_local_cleanup: Any
    _channels: dict
    _channel_health: dict
    waiters: dict
    _last_notification: Any
    _notification_queue_size: int
    _start_time: float
    authentication: Any

    def workspace_instance_id(self) -> str:
        return hashlib.sha256(
            f"{self.tool_id}:{self.port}".encode("utf-8")
        ).hexdigest()[:16]

    @property
    def role(self) -> str:
        return "governed-tool-runtime"

    async def _run_local_self_repair(self) -> dict[str, Any]:
        from .tool_self_repair import run_local_self_repair

        try:
            result = await asyncio.to_thread(
                run_local_self_repair,
                self.tool_id,
                self.tool_root,
                clear_pycache=self.self_repair_clear_pycache,
            )
        except (OSError, ValueError, RuntimeError, ImportError, TypeError) as error:
            result = {
                "ok": False,
                "operation": "local-self-repair",
                "authority": "tool-local",
                "tool_id": self.tool_id,
                "database_errors": [str(error)],
                "errors": [str(error)],
            }
        self._last_self_repair = result
        return result

    def _self_repair_health(self) -> dict[str, Any]:
        last = self._last_self_repair or {}
        return {
            "self_repair": {
                "enabled": self.self_repair_enabled,
                "completed": self._last_self_repair is not None,
                "last_ok": last.get("ok"),
                "checked_databases": last.get("checked_databases") or [],
                "preserved_databases": last.get("preserved_databases") or [],
                "database_errors": last.get("database_errors") or [],
            }
        }

    async def _run_local_cleanup(self) -> dict[str, Any]:
        from .tool_local_cleanup import (
            run_local_cleanup,
            write_local_cleanup_state,
        )

        try:
            result = await asyncio.to_thread(
                run_local_cleanup,
                self.tool_id,
                self.tool_root,
            )
        except (OSError, ValueError, RuntimeError, ImportError, TypeError) as error:
            result = {
                "ok": False,
                "operation": "local-self-cleanup",
                "authority": "tool-local",
                "tool_id": self.tool_id,
                "cleaned_files": [],
                "cleaned_directories": [],
                "skipped": [{"reason": f"{type(error).__name__}: {error}"}],
                "cleaned_bytes": 0,
            }
        self._last_local_cleanup = result
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                write_local_cleanup_state,
                self.tool_root,
                result,
            )
        return result

    def _local_cleanup_health(self) -> dict[str, Any]:
        last = self._last_local_cleanup or {}
        return {
            "local_cleanup": {
                "enabled": self.local_cleanup_enabled,
                "completed": self._last_local_cleanup is not None,
                "last_ok": last.get("ok"),
                "cleaned_files": last.get("cleaned_files") or [],
                "cleaned_directories": last.get("cleaned_directories") or [],
                "cleaned_bytes": last.get("cleaned_bytes") or 0,
            }
        }

    def _collect_channel_metrics(self) -> dict[str, Any]:
        """Collect channel performance metrics for monitoring."""
        metrics = {
            "channel_health": {
                channel_id: health.as_dict()
                for channel_id, health in self._channel_health.items()
            },
            "worker_queue_size": sum(1 for _ in self.waiters.values()),
            "processing_channels": list(self._processing_channel_ids),
            "notification_queue_size": getattr(self, '_notification_queue_size', 0),
            "last_notification": self._last_notification,
            "uptime_seconds": time.monotonic() - getattr(self, '_start_time', time.monotonic()),
        }
        if hasattr(self, '_gateway') and self._gateway is not None:
            metrics["gateway"] = self._gateway.get_metrics_snapshot()
        return metrics

    def health_snapshot(self) -> dict[str, Any]:
        extra = self.health_callback() if self.health_callback else {}
        return {
            "ok": True,
            "role": self.role,
            "sovereign_id": self.sovereign_id,
            "authority": SUB_SOVEREIGN_AUTHORITY,
            "scope": SUB_SOVEREIGN_SCOPE,
            "duty": list(SUB_SOVEREIGN_DUTY),
            "subordinate_to": list(SUB_SOVEREIGN_UNDER),
            "version": self.version,
            "tool_id": self.tool_id,
            "runtime_scope": "independent-tool",
            "governance_ready": True,
            "workspace_instance_id": self.workspace_instance_id(),
            "channels": sorted(self._channels),
            "channel_routes": {
                channel_id: f"{channel_id}-channel/{self.tool_id}"
                for channel_id in self._channels
            },
            "channel_health": {
                channel_id: health.as_dict()
                for channel_id, health in self._channel_health.items()
            },
            "_self_repair": self._self_repair_health()
            if self.self_repair_enabled
            else {},
            "_local_cleanup": self._local_cleanup_health()
            if self.local_cleanup_enabled
            else {},
            **extra,
        }

    async def run(self) -> None:
        worker: asyncio.Task[Any] | None = None

        def process_request(_connection: Any, request: Any):
            request_path = urlsplit(str(request.path))
            if request_path.path == "/health":
                body = json.dumps(self.health_snapshot()).encode("utf-8")
                return _http_response(200, "OK", body, "application/json")
            if request_path.path == "/metrics":
                metrics = self._collect_channel_metrics()
                body = json.dumps(metrics, ensure_ascii=False, indent=2).encode("utf-8")
                return _http_response(200, "OK", body, "application/json")
            if request_path.path == "/shutdown":
                supplied = str(request.headers.get("X-GPTBridge-Shutdown-Token") or "")
                if not self.shutdown_token or not hmac.compare_digest(
                    supplied, self.shutdown_token
                ):
                    return _http_response(403, "FORBIDDEN", b"Forbidden", "text/plain")
                self.shutdown_event.set()
                return _http_response(200, "OK", b"OK", "text/plain")
            query = parse_qs(request_path.query)
            supplied_token = str((query.get("token") or [""])[0]).lower()
            supplied_instance = str((query.get("instance") or [""])[0])
            if (
                not hmac.compare_digest(supplied_token, self.token)
                or supplied_instance != self.workspace_instance_id()
            ):
                return _http_response(403, "FORBIDDEN", b"Forbidden", "text/plain")
            return None

        try:
            if self.self_repair_enabled:
                await self._run_local_self_repair()
            if self.local_cleanup_enabled:
                await self._run_local_cleanup()
            if self.startup_callback is not None:
                await self.startup_callback()
            worker = asyncio.create_task(self._worker())
            async with websockets.serve(
                self._handler,
                "127.0.0.1",
                self.port,
                origins=(None, "file://", "null"),
                process_request=process_request,
            ):
                await self.shutdown_event.wait()
        finally:
            if self.shutdown_callback is not None:
                await self.shutdown_callback()
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            if self.idle_cleanup is not None:
                await asyncio.to_thread(self.idle_cleanup)
            self.authentication.close()


__all__ = ["GovernedRuntimeMaintenanceMixin"]
