"""Enhanced Update Manager — facade.

This module provides the UpdateManager class.  Implementation details
live in submodules:

  * :mod:`core_system.update_manager_types` — types, enums, dataclasses.
  * :mod:`core_system.update_manager_health` — UpdateHealthMonitor.
  * :mod:`core_system.update_manager_history` — UpdateHistory.

Law basis: A181/A182/A183 (hot-update boundary), A191/A192 (parallel
update safety), A330 (certified update)
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import sys
import threading
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from core_system.hot_update_service import HotUpdateService
from core_system.active_release import resolve_active_pointer
from core_system.update_manager_types import (
    HealthCheckResult,
    UpdateManifest,
    UpdateStatus,
    UpdateType,
)
from core_system.update_manager_health import UpdateHealthMonitor
from core_system.update_manager_history import UpdateHistory
from core_system.update_manager_execution import UpdateExecutionMixin

_logger = logging.getLogger("gptbridge.update_manager")


class UpdateManager(UpdateExecutionMixin):
    """Enhanced update manager with version tracking, health monitoring, and automated recovery."""

    def __init__(
        self,
        app: Any,
        hot_update_service: HotUpdateService,
        project_root: Optional[Path] = None,
    ) -> None:
        self.app = app
        self.hot_update = hot_update_service
        self.project_root = project_root or Path(__file__).resolve().parents[3]
        self.history = UpdateHistory(self.project_root)
        self.health = UpdateHealthMonitor(app, self.project_root)

        self._current_manifest: Optional[UpdateManifest] = None
        self._lock = threading.RLock()
        self._auto_update_task: Optional[asyncio.Task] = None
        self._stop_auto = asyncio.Event()
        self._update_callbacks: list[Callable[[UpdateManifest], None]] = []
        # Source hash tracking for auto-update change detection.
        self._source_hashes: dict[str, str] = {}

        # Configuration
        self.auto_update_enabled = True
        self.check_interval_seconds = 300.0  # Check for updates every 5 minutes (was 60s)
        self.max_concurrent_updates = 1
        self.health_check_timeout = 30.0

        # Adaptive interval: increase when stable, decrease when changes detected
        self._adaptive_interval = self.check_interval_seconds
        self._consecutive_no_changes = 0
        self._max_adaptive_interval = 1800.0  # 30 minutes
        self._min_adaptive_interval = 60.0    # 1 minute

        # Circuit breaker for consecutive failures
        self._consecutive_failures = 0
        self._circuit_breaker_threshold = 5
        self._circuit_open_until = 0.0

        # Metrics
        self._update_count = 0
        self._failed_count = 0
        self._rollback_count = 0
        self._last_check_time: float = 0.0
        self._last_successful_update: float = 0.0
        self._total_check_duration: float = 0.0
        self._check_count: int = 0

    def register_callback(self, callback: Callable[[UpdateManifest], None]) -> None:
        """Register a callback for update status changes."""
        with self._lock:
            self._update_callbacks.append(callback)

    def unregister_callback(self, callback: Callable[[UpdateManifest], None]) -> None:
        """Unregister a callback."""
        with self._lock:
            if callback in self._update_callbacks:
                self._update_callbacks.remove(callback)

    def _notify_callbacks(self, manifest: UpdateManifest) -> None:
        """Notify all registered callbacks."""
        with self._lock:
            callbacks = list(self._update_callbacks)
        for cb in callbacks:
            try:
                cb(manifest)
            except Exception:
                pass

    def _create_manifest(
        self,
        update_type: UpdateType,
        modules: Optional[list[str]] = None,
    ) -> UpdateManifest:
        """Create a new update manifest."""
        update_id = f"upd-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{os.urandom(4).hex()}"
        return UpdateManifest(
            update_id=update_id,
            update_type=update_type,
            status=UpdateStatus.IDLE,
            started_at=datetime.now(timezone.utc).isoformat(),
            modules=modules or [],
        )

    def _update_status(self, manifest: UpdateManifest, status: UpdateStatus,
                       error: Optional[str] = None) -> None:
        """Update manifest status and notify."""
        manifest.status = status
        if error:
            manifest.error = error
        if status in (UpdateStatus.COMPLETED, UpdateStatus.FAILED, UpdateStatus.ROLLED_BACK):
            manifest.completed_at = datetime.now(timezone.utc).isoformat()
            start = datetime.fromisoformat(manifest.started_at.replace("Z", "+00:00"))
            end = datetime.now(timezone.utc)
            manifest.duration_ms = int((end - start).total_seconds() * 1000)
            if status == UpdateStatus.COMPLETED:
                self._update_count += 1
                self._last_successful_update = time.monotonic()
            elif status == UpdateStatus.FAILED:
                self._failed_count += 1
            elif status == UpdateStatus.ROLLED_BACK:
                self._rollback_count += 1
        self.history.update(manifest)
        self._notify_callbacks(manifest)

    async def _rollback(self, manifest: UpdateManifest) -> None:
        """Perform rollback of the last update."""
        manifest.rollback_reason = manifest.error
        try:
            # The hot_update_service has built-in rollback on failure
            # Here we just verify the system is back to healthy state
            await asyncio.sleep(1.0)
            health = await self.health.run_all_checks()
            if not all(health.values()):
                _logger.error("Rollback completed but health checks still failing")
            else:
                _logger.info("Rollback completed successfully")
        except Exception as e:
            _logger.error(f"Rollback failed: {e}")

    async def start_auto_update(self) -> None:
        """Start the governed end-to-end automatic update workflow."""
        from core_system.auto_action_policy import (
            automatic_update_execution_allowed,
        )

        if not automatic_update_execution_allowed():
            _logger.info(
                "Auto-update workflow disabled by the Xingcheng Assistant switch"
            )
            return
        if self._auto_update_task is not None and not self._auto_update_task.done():
            return
        self._stop_auto.clear()
        self._auto_update_task = asyncio.create_task(
            self._auto_update_loop(),
            name="update-manager-auto-loop"
        )

    async def stop_auto_update(self) -> None:
        """Stop automatic update checking."""
        self._stop_auto.set()
        if self._auto_update_task is not None:
            self._auto_update_task.cancel()
            try:
                await self._auto_update_task
            except asyncio.CancelledError:
                pass
        self._auto_update_task = None

    def get_status(self) -> dict:
        """Get current update manager status."""
        return {
            "auto_update_enabled": self.auto_update_enabled,
            "current_update": self._current_manifest.to_dict() if self._current_manifest else None,
            "history_stats": self.history.get_stats(),
            "health": self.health.get_last_results(),
            "healthy": self.health.is_healthy(),
            "metrics": {
                "total_updates": self._update_count,
                "failed_updates": self._failed_count,
                "rollbacks": self._rollback_count,
                "avg_check_duration_ms": round(self._total_check_duration / max(self._check_count, 1) * 1000, 2),
                "last_check_seconds_ago": round(time.monotonic() - self._last_check_time, 1) if self._last_check_time else None,
                "last_successful_update_seconds_ago": round(time.monotonic() - self._last_successful_update, 1) if self._last_successful_update else None,
                "adaptive_interval_seconds": round(self._adaptive_interval, 1),
                "consecutive_failures": self._consecutive_failures,
                "circuit_breaker_open": time.time() < self._circuit_open_until,
            }
        }

    def get_history(self, count: int = 20) -> list[dict]:
        """Get recent update history."""
        return [m.to_dict() for m in self.history.get_recent(count)]

    async def stop(self) -> None:
        """Stop the update manager."""
        await self.stop_auto_update()
        _logger.info("UpdateManager stopped")


__all__ = [
    "UpdateManager",
    "UpdateManifest",
    "UpdateStatus",
    "UpdateType",
    "UpdateHealthMonitor",
    "UpdateHistory",
    "HealthCheckResult",
]
