"""Update health monitor — A181/A182/A183 health checking.

Monitors system health before, during, and after updates.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from core_system.active_release import resolve_active_pointer
from core_system.update_manager_types import HealthCheckResult
from core_system.update_manager_health_checks import UpdateHealthChecksMixin

_logger = logging.getLogger("gptbridge.update_manager")


class UpdateHealthMonitor(UpdateHealthChecksMixin):
    """Monitors system health before, during, and after updates."""

    def __init__(self, app: Any, project_root: Path) -> None:
        self.app = app
        self.project_root = project_root
        self._checks: dict[str, Callable[[], HealthCheckResult]] = {}
        self._last_results: dict[str, HealthCheckResult] = {}
        self._lock = threading.RLock()
        self._register_default_checks()

    def _register_default_checks(self) -> None:
        """Register default health checks."""
        self.register_check("active_release", self._check_active_release)
        self.register_check("governance", self._check_governance)
        self.register_check("ipc_server", self._check_ipc_server)
        self.register_check("sovereign_stack", self._check_sovereign_stack)
        self.register_check("module_integrity", self._check_module_integrity)
        self.register_check("maintenance_ready", self._check_maintenance_ready)
        self.register_check("websocket_connectivity", self._check_websocket)
        self.register_check("governance_audit", self._check_governance_audit)
        self.register_check("update_channel", self._check_update_channel)

    def register_check(self, name: str, check_fn: Callable[[], HealthCheckResult]) -> None:
        """Register a custom health check."""
        with self._lock:
            self._checks[name] = check_fn

    def unregister_check(self, name: str) -> None:
        """Unregister a health check."""
        with self._lock:
            self._checks.pop(name, None)

    async def run_all_checks(self) -> dict[str, HealthCheckResult]:
        """Run all registered health checks."""
        results = {}
        with self._lock:
            check_names = list(self._checks.keys())

        for name in check_names:
            try:
                start = time.monotonic()
                check_fn = self._checks.get(name)
                if check_fn:
                    result = await asyncio.to_thread(check_fn)
                else:
                    result = HealthCheckResult(name, False, "Check function not found",
                                             datetime.now(timezone.utc).isoformat(), 0)
            except Exception as e:
                result = HealthCheckResult(name, False, f"Check failed: {e}",
                                         datetime.now(timezone.utc).isoformat(), 0)

            result.duration_ms = int((time.monotonic() - start) * 1000)
            results[name] = result

        with self._lock:
            self._last_results.update(results)

        return results

    def get_last_results(self) -> dict[str, HealthCheckResult]:
        """Get last health check results."""
        with self._lock:
            return dict(self._last_results)

    def is_healthy(self) -> bool:
        """Check if all last checks passed."""
        with self._lock:
            return all(r.passed for r in self._last_results.values())

    def _check_active_release(self) -> HealthCheckResult:
        """Verify active release pointer is valid."""
        try:
            pointer = resolve_active_pointer()
            if pointer is None:
                return HealthCheckResult(
                    "active_release", False, "No active release pointer",
                    datetime.now(timezone.utc).isoformat(), 0
                )
            return HealthCheckResult(
                "active_release", True, f"Active release: {pointer.release_id}",
                datetime.now(timezone.utc).isoformat(), 0
            )
        except Exception as e:
            return HealthCheckResult(
                "active_release", False, f"Error: {e}",
                datetime.now(timezone.utc).isoformat(), 0
            )

    def _check_governance(self) -> HealthCheckResult:
        """Verify governance is available and healthy."""
        try:
            governance = getattr(self.app, "governance", None)
            if governance is None:
                return HealthCheckResult(
                    "governance", False, "Governance not initialized",
                    datetime.now(timezone.utc).isoformat(), 0
                )
            if hasattr(governance, "runtime_integrity_ready"):
                healthy = governance.runtime_integrity_ready(max_age_seconds=30)
                return HealthCheckResult(
                    "governance", healthy,
                    "Governance integrity OK" if healthy else "Governance integrity stale",
                    datetime.now(timezone.utc).isoformat(), 0
                )
            return HealthCheckResult(
                "governance", True, "Governance available",
                datetime.now(timezone.utc).isoformat(), 0
            )
        except Exception as e:
            return HealthCheckResult(
                "governance", False, f"Error: {e}",
                datetime.now(timezone.utc).isoformat(), 0
            )

    def _check_ipc_server(self) -> HealthCheckResult:
        """Verify IPC server is responsive."""
        try:
            import urllib.request
            health_port = os.environ.get("GPTBRIDGE_HEALTH_PORT", "8765")
            url = f"http://127.0.0.1:{health_port}/health?level=brief"
            request = urllib.request.Request(url, headers={"Connection": "close"})
            _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with _opener.open(request, timeout=3) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                ok = payload.get("ok") is True
                return HealthCheckResult(
                    "ipc_server", ok,
                    "IPC server responsive" if ok else "IPC server unhealthy",
                    datetime.now(timezone.utc).isoformat(), 0
                )
        except Exception as e:
            return HealthCheckResult(
                "ipc_server", False, f"Error: {e}",
                datetime.now(timezone.utc).isoformat(), 0
            )

    def _check_sovereign_stack(self) -> HealthCheckResult:
        """Verify sovereign stack is initialized."""
        try:
            required = ["decision_sovereign", "permission_sovereign", "system_runtime_sovereign",
                       "automation_sovereign", "xingcheng_sovereign"]
            missing = [s for s in required if not hasattr(self.app, s) or getattr(self.app, s) is None]
            if missing:
                return HealthCheckResult(
                    "sovereign_stack", False, f"Missing sovereigns: {missing}",
                    datetime.now(timezone.utc).isoformat(), 0
                )
            return HealthCheckResult(
                "sovereign_stack", True, "All sovereigns initialized",
                datetime.now(timezone.utc).isoformat(), 0
            )
        except Exception as e:
            return HealthCheckResult(
                "sovereign_stack", False, f"Error: {e}",
                datetime.now(timezone.utc).isoformat(), 0
            )

    def _check_module_integrity(self) -> HealthCheckResult:
        """Verify critical modules are loadable."""
        try:
            critical = [
                "core_system.hot_update_service",
                "core_system.active_release",
                "core_system.governance_runtime",
                "governance.sovereigns",
            ]
            for mod_name in critical:
                try:
                    __import__(mod_name)
                except ImportError:
                    return HealthCheckResult(
                        "module_integrity", False, f"Cannot import {mod_name}",
                        datetime.now(timezone.utc).isoformat(), 0
                    )
            return HealthCheckResult(
                "module_integrity", True, "Critical modules importable",
                datetime.now(timezone.utc).isoformat(), 0
            )
        except Exception as e:
            return HealthCheckResult(
                "module_integrity", False, f"Error: {e}",
                datetime.now(timezone.utc).isoformat(), 0
            )

    def _check_maintenance_ready(self) -> HealthCheckResult:
        """Verify the maintenance sovereign has reported readiness.

        Per A152/A154 the maintenance sovereign owns health-only scope and
        must report readiness before the system is considered healthy.
        """
        try:
            ready = bool(getattr(self.app, "maintenance_ready", False))
            if ready:
                return HealthCheckResult(
                    "maintenance_ready", True, "Maintenance sovereign ready",
                    datetime.now(timezone.utc).isoformat(), 0
                )
            return HealthCheckResult(
                "maintenance_ready", False,
                "Maintenance sovereign has not reported readiness",
                datetime.now(timezone.utc).isoformat(), 0
            )
        except Exception as e:
            return HealthCheckResult(
                "maintenance_ready", False, f"Error: {e}",
                datetime.now(timezone.utc).isoformat(), 0
            )

    def _check_websocket(self) -> HealthCheckResult:
        """Verify the backend WebSocket server accepts connections."""
        import socket
        try:
            ipc_port = int(os.environ.get("GPTBRIDGE_IPC_PORT", "8765"))
            with socket.create_connection(
                ("127.0.0.1", ipc_port), timeout=2.0
            ) as sock:
                pass
            return HealthCheckResult(
                "websocket_connectivity", True,
                f"WebSocket server listening on port {ipc_port}",
                datetime.now(timezone.utc).isoformat(), 0
            )
        except OSError as e:
            return HealthCheckResult(
                "websocket_connectivity", False,
                f"Cannot connect to WebSocket server: {e}",
                datetime.now(timezone.utc).isoformat(), 0
            )


__all__ = ["UpdateHealthMonitor"]
