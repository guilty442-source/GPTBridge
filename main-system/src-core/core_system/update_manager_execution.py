"""Update execution mixin (A185 split).

Contains the check_and_update, _auto_update_loop, and
_detect_source_changes methods extracted from UpdateManager.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
import time
import types
from pathlib import Path
from typing import Optional

from core_system.active_release import resolve_active_pointer
from core_system.update_manager_types import (
    UpdateManifest,
    UpdateStatus,
    UpdateType,
)

_logger = logging.getLogger("gptbridge.update_manager")


class UpdateExecutionMixin:
    """Update execution and auto-update loop."""

    app: object
    project_root: Path
    health: object
    history: object
    _current_manifest: Optional[UpdateManifest]
    _stop_auto: object
    _consecutive_failures: int
    _circuit_breaker_threshold: int
    _circuit_open_until: float
    _adaptive_interval: float
    _min_adaptive_interval: float
    _max_adaptive_interval: float
    _consecutive_no_changes: int
    _source_hashes: dict[str, str]

    def _create_manifest(self, update_type: UpdateType, modules: Optional[list[str]]) -> UpdateManifest:
        raise NotImplementedError

    def _update_status(self, manifest: UpdateManifest, status: UpdateStatus, message: str = "") -> None:
        raise NotImplementedError

    async def _rollback(self, manifest: UpdateManifest) -> None:
        raise NotImplementedError

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

    async def _auto_update_loop(self) -> None:
        """Automatic update checking loop."""
        while not self._stop_auto.is_set():
            try:
                # Circuit breaker: if too many consecutive failures, back off
                if self._consecutive_failures >= self._circuit_breaker_threshold:
                    if time.time() < self._circuit_open_until:
                        _logger.warning(
                            "Auto-update circuit breaker open, waiting %.0fs",
                            self._circuit_open_until - time.time(),
                        )
                        await asyncio.sleep(60)
                        continue
                    else:
                        self._consecutive_failures = 0
                        self._circuit_open_until = 0
                        _logger.info("Auto-update circuit breaker reset")

                # User-confirmation gate: refresh the hash baseline but do
                # not emit automatic updates while execution is disabled.
                from core_system.auto_action_policy import (
                    automatic_update_execution_allowed,
                )

                if not automatic_update_execution_allowed():
                    await asyncio.to_thread(self._detect_source_changes)
                    await asyncio.sleep(self._adaptive_interval)
                    continue

                # Pre-update health check — only trigger updates when healthy.
                health = await self.health.run_all_checks()
                if not all(h.passed for h in health.values()):
                    await asyncio.sleep(self._adaptive_interval)
                    continue

                # Detect source changes by comparing hashes.
                changed = await asyncio.to_thread(self._detect_source_changes)
                if changed:
                    _logger.info(
                        "Auto-update: %d source modules changed: %s",
                        len(changed),
                        ", ".join(sorted(changed)[:8]),
                    )
                    # Reset adaptive interval on changes
                    self._consecutive_no_changes = 0
                    self._adaptive_interval = self._min_adaptive_interval
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
                    # Adaptive interval: increase when stable
                    self._consecutive_no_changes += 1
                    if self._consecutive_no_changes >= 3:
                        self._adaptive_interval = min(
                            self._adaptive_interval * 1.5,
                            self._max_adaptive_interval,
                        )

            except Exception as e:
                _logger.error(f"Auto-update check failed: {e}")
                self._consecutive_failures += 1
                if self._consecutive_failures >= self._circuit_breaker_threshold:
                    self._circuit_open_until = time.time() + 300  # 5 minutes
                    _logger.warning("Auto-update circuit breaker opened for 5 minutes")

            try:
                await asyncio.sleep(self._adaptive_interval)
            except asyncio.CancelledError:
                break

    def _detect_source_changes(self) -> set[str]:
        """Detect source-file changes by comparing SHA-256 hashes."""
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


__all__ = ["UpdateExecutionMixin"]
