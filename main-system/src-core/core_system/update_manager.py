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

_logger = logging.getLogger("gptbridge.update_manager")


class UpdateManager:
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
        self.check_interval_seconds = 60.0  # Check for updates every 60s
        self.max_concurrent_updates = 1
        self.health_check_timeout = 30.0

        # Metrics
        self._update_count = 0
        self._failed_count = 0
        self._rollback_count = 0

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
            elif status == UpdateStatus.FAILED:
                self._failed_count += 1
            elif status == UpdateStatus.ROLLED_BACK:
                self._rollback_count += 1
        self.history.update(manifest)
        self._notify_callbacks(manifest)

    async def check_and_update(
        self,
        modules: Optional[list[str]] = None,
        update_type: UpdateType = UpdateType.HOT_RELOAD,
    ) -> UpdateManifest:
        """Check for updates and apply if available."""
        manifest = self._create_manifest(update_type, modules)
        self._current_manifest = manifest
        self.history.add(manifest)
        self._update_status(manifest, UpdateStatus.CHECKING)

        try:
            # Pre-update health check
            self._update_status(manifest, UpdateStatus.PREPARING)
            health_results = await self.health.run_all_checks()
            manifest.health_checks = {k: v.passed for k, v in health_results.items()}

            if not all(health_results.values()):
                failed = [k for k, v in health_results.items() if not v.passed]
                self._update_status(manifest, UpdateStatus.FAILED,
                                  f"Pre-update health check failed: {failed}")
                return manifest

            # Prepare update
            self._update_status(manifest, UpdateStatus.PREPARING)
            governance = getattr(self.app, "governance", None)
            if governance is None:
                self._update_status(manifest, UpdateStatus.FAILED, "Governance unavailable")
                return manifest

            # Prepare generation
            hot_update = getattr(self.app, "hot_update_service", None)
            if hot_update is None or not hasattr(hot_update, "prepare_generation"):
                self._update_status(manifest, UpdateStatus.FAILED, "Hot update service unavailable")
                return manifest

            module_set = set(modules) if modules else None
            prepared = await asyncio.to_thread(
                hot_update.prepare_generation,
                governance=governance,
                modules=module_set,
                standby_validation=True,
            )
            if not getattr(prepared, "ok", False):
                self._update_status(manifest, UpdateStatus.FAILED,
                                  f"Prepare failed: {getattr(prepared, 'error', 'unknown')}")
                return manifest

            manifest.module_hashes = getattr(prepared, "hashes", {})

            # Execute reload
            self._update_status(manifest, UpdateStatus.RELOADING)
            result = await asyncio.to_thread(
                hot_update.reload_modules,
                governance=governance,
                modules=module_set,
            )

            # Post-reload health check
            self._update_status(manifest, UpdateStatus.VALIDATING)
            post_health = await self.health.run_all_checks()
            manifest.health_checks.update({k: v.passed for k, v in post_health.items()})

            # Verify active release
            pointer = resolve_active_pointer()
            if pointer:
                manifest.post_reload_pointer = {
                    "release_id": pointer.release_id,
                    "certificate_digest": pointer.certificate_digest,
                }

            if result.ok and all(post_health.values()):
                self._update_status(manifest, UpdateStatus.COMPLETED)
                manifest.modules = getattr(result, "reloaded", [])
            else:
                # Rollback
                self._update_status(manifest, UpdateStatus.ROLLING_BACK)
                await self._rollback(manifest)
                self._update_status(manifest, UpdateStatus.ROLLED_BACK,
                                  f"Update failed: {getattr(result, 'error', 'health check failed')}")

        except Exception as e:
            _logger.exception("Update failed")
            self._update_status(manifest, UpdateStatus.FAILED, str(e))
            await self._rollback(manifest)

        return manifest

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
        """Start automatic update checking.

        User directive: automatic updates must not execute without explicit
        user confirmation.
        """
        from core_system.auto_action_policy import (
            automatic_update_execution_allowed,
        )

        if not automatic_update_execution_allowed():
            _logger.info(
                "Auto-update loop disabled: updates await user confirmation"
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

    async def _auto_update_loop(self) -> None:
        """Automatic update checking loop.

        Detects source-file changes by comparing SHA-256 hashes against
        the last-known set.
        """
        while not self._stop_auto.is_set():
            try:
                # User-confirmation gate: refresh the hash baseline but do
                # not emit automatic updates while execution is disabled.
                from core_system.auto_action_policy import (
                    automatic_update_execution_allowed,
                )

                if not automatic_update_execution_allowed():
                    await asyncio.to_thread(self._detect_source_changes)
                    await asyncio.sleep(self.check_interval_seconds)
                    continue

                # Pre-update health check — only trigger updates when healthy.
                health = await self.health.run_all_checks()
                if not all(h.passed for h in health.values()):
                    await asyncio.sleep(self.check_interval_seconds)
                    continue

                # Detect source changes by comparing hashes.
                changed = await asyncio.to_thread(self._detect_source_changes)
                if changed:
                    _logger.info(
                        "Auto-update: %d source modules changed: %s",
                        len(changed),
                        ", ".join(sorted(changed)[:8]),
                    )
                    # Route through the hot_reload_watcher.
                    watcher = getattr(self.app, "hot_reload_watcher", None)
                    if watcher is not None:
                        changed_paths = [
                            str(sys.modules[name].__file__)
                            for name in changed
                            if name in sys.modules
                            and hasattr(sys.modules[name], "__file__")
                            and sys.modules[name].__file__
                        ]
                        if changed_paths:
                            await asyncio.to_thread(
                                lambda: asyncio.run(watcher._maybe_reload(changed_paths))
                            )
                    else:
                        # Fall back to direct check_and_update if no watcher.
                        await self.check_and_update(
                            modules=list(changed),
                            update_type=UpdateType.HOT_RELOAD,
                        )
                else:
                    _logger.debug("Auto-update check completed — no changes")

            except Exception as e:
                _logger.error(f"Auto-update check failed: {e}")

            try:
                await asyncio.sleep(self.check_interval_seconds)
            except asyncio.CancelledError:
                break

    def _detect_source_changes(self) -> set[str]:
        """Detect source-file changes by comparing SHA-256 hashes.

        Scans loaded modules whose ``__file__`` is under the governed
        src roots and compares against the last-known hash set.
        """
        import hashlib
        from pathlib import Path
        try:
            from core_system.hot_update_service import (
                RELOADABLE_SRC_ROOTS,
                _is_protected,
            )
        except ImportError:
            return set()

        project_root = self.project_root.resolve()
        src_roots = [
            (project_root / root).resolve()
            for root in RELOADABLE_SRC_ROOTS
        ]
        changed: set[str] = set()
        new_hashes: dict[str, str] = {}
        for name, module in list(sys.modules.items()):
            if not isinstance(module, types.ModuleType):
                continue
            if _is_protected(name):
                continue
            file_path = getattr(module, "__file__", None)
            if not file_path:
                continue
            resolved = Path(file_path).resolve()
            if not any(
                str(resolved).startswith(str(root))
                for root in src_roots
            ):
                continue
            try:
                digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
            except OSError:
                continue
            new_hashes[name] = digest
            old = self._source_hashes.get(name)
            if old is not None and old != digest:
                changed.add(name)
        # Update the stored hashes for next cycle.
        self._source_hashes = new_hashes
        return changed

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
