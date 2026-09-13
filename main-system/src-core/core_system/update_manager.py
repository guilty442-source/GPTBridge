"""Enhanced Update Manager — Self-update capability with version tracking, health monitoring, and automated recovery.

Law basis: A181/A182/A183 (hot-update boundary), A191/A192 (parallel update safety), A330 (certified update)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
import time
import types
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional
from collections import deque

from core_system.hot_update_service import HotUpdateService
from core_system.active_release import resolve_active_pointer

_logger = logging.getLogger("gptbridge.update_manager")


class UpdateStatus(Enum):
    """Update lifecycle status."""
    IDLE = "idle"
    CHECKING = "checking"
    PREPARING = "preparing"
    RELOADING = "reloading"
    VALIDATING = "validating"
    COMPLETED = "completed"
    FAILED = "failed"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class UpdateType(Enum):
    """Type of update."""
    HOT_RELOAD = "hot_reload"
    GENERATION_PREPARE = "generation_prepare"
    CERTIFIED_UPDATE = "certified_update"
    MANUAL = "manual"


@dataclass
class UpdateManifest:
    """Manifest tracking an update operation."""
    update_id: str
    update_type: UpdateType
    status: UpdateStatus
    started_at: str
    completed_at: Optional[str] = None
    modules: list[str] = field(default_factory=list)
    module_hashes: dict[str, str] = field(default_factory=dict)
    pre_reload_pointer: Optional[dict] = None
    post_reload_pointer: Optional[dict] = None
    error: Optional[str] = None
    health_checks: dict[str, bool] = field(default_factory=dict)
    rollback_reason: Optional[str] = None
    duration_ms: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HealthCheckResult:
    """Result of a health check."""
    name: str
    passed: bool
    message: str
    timestamp: str
    duration_ms: int


class UpdateHealthMonitor:
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

    # Default health checks
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
                import json
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
                       "synchronization_sovereign", "xingcheng_sovereign"]
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
        This check fails when ``maintenance_ready`` is False, which
        indicates the maintenance sovereign has not completed its startup
        or has crashed.
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
        """Verify the backend WebSocket server accepts connections.

        Opens a raw TCP socket to the IPC port and verifies the server
        accepts the connection.  A full WebSocket handshake is not
        performed (that requires an authenticated session token), but a
        refused or timed-out TCP connection definitively indicates the
        WebSocket server is down.
        """
        import socket
        try:
            ipc_port = int(os.environ.get("GPTBRIDGE_IPC_PORT", "8765"))
            with socket.create_connection(
                ("127.0.0.1", ipc_port), timeout=2.0
            ) as sock:
                # The WebSocket server accepts the TCP connection; that's
                # sufficient to know it's listening.  Close immediately.
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

    def _check_governance_audit(self) -> HealthCheckResult:
        """Verify the governance audit passes.

        Runs ``governance_rule.execution.audit`` in-process and checks for
        the ``[PASS]`` marker.  A failed audit indicates the system's
        governance state is inconsistent — this is a critical health
        signal that must block updates.
        """
        import subprocess
        try:
            project_root = self.project_root
            result = subprocess.run(
                [
                    sys.executable, "-m",
                    "governance_rule.execution.audit",
                ],
                cwd=str(project_root),
                capture_output=True,
                text=True,
                timeout=15.0,
            )
            output = (result.stdout or "") + (result.stderr or "")
            passed = "[PASS]" in output and result.returncode == 0
            if passed:
                return HealthCheckResult(
                    "governance_audit", True, "Governance audit passed",
                    datetime.now(timezone.utc).isoformat(), 0
                )
            return HealthCheckResult(
                "governance_audit", False,
                f"Governance audit failed: {output.strip()[:200]}",
                datetime.now(timezone.utc).isoformat(), 0
            )
        except subprocess.TimeoutExpired:
            return HealthCheckResult(
                "governance_audit", False, "Governance audit timed out",
                datetime.now(timezone.utc).isoformat(), 0
            )
        except Exception as e:
            return HealthCheckResult(
                "governance_audit", False, f"Error: {e}",
                datetime.now(timezone.utc).isoformat(), 0
            )


class UpdateHistory:
    """Tracks update history with persistence."""

    def __init__(self, project_root: Path, max_entries: int = 100) -> None:
        self.project_root = project_root
        self.max_entries = max_entries
        self._history_file = project_root / "main-system" / "runtime" / "state" / "update-history.json"
        self._history: deque[UpdateManifest] = deque(maxlen=max_entries)
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        """Load history from file."""
        try:
            if self._history_file.is_file():
                data = json.loads(self._history_file.read_text(encoding="utf-8"))
                with self._lock:
                    self._history.clear()
                    for entry in data.get("history", []):
                        entry["update_type"] = UpdateType(entry["update_type"])
                        entry["status"] = UpdateStatus(entry["status"])
                        self._history.append(UpdateManifest(**entry))
        except Exception:
            pass

    def _save(self) -> None:
        """Save history to file."""
        try:
            self._history_file.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {
                    "version": 1,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "history": [entry.to_dict() for entry in self._history]
                }
                tmp = self._history_file.with_suffix(".tmp")
                tmp.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8"
                )
                os.replace(tmp, self._history_file)
        except Exception:
            pass

    def add(self, manifest: UpdateManifest) -> None:
        """Add an update manifest to history."""
        with self._lock:
            self._history.append(manifest)
        self._save()

    def update(self, manifest: UpdateManifest) -> None:
        """Update an existing manifest in history."""
        with self._lock:
            for i, m in enumerate(self._history):
                if m.update_id == manifest.update_id:
                    self._history[i] = manifest
                    break
        self._save()

    def get_recent(self, count: int = 20) -> list[UpdateManifest]:
        """Get recent update history."""
        with self._lock:
            return list(self._history)[-count:]

    def get_by_id(self, update_id: str) -> Optional[UpdateManifest]:
        """Get manifest by update ID."""
        with self._lock:
            for m in self._history:
                if m.update_id == update_id:
                    return m
        return None

    def get_stats(self) -> dict:
        """Get update statistics."""
        with self._lock:
            total = len(self._history)
            if total == 0:
                return {"total": 0, "success_rate": 0.0, "avg_duration_ms": 0}
            successful = sum(1 for m in self._history if m.status == UpdateStatus.COMPLETED)
            failed = sum(1 for m in self._history if m.status == UpdateStatus.FAILED)
            rolled_back = sum(1 for m in self._history if m.status == UpdateStatus.ROLLED_BACK)
            durations = [m.duration_ms for m in self._history if m.duration_ms > 0]
            avg_duration = sum(durations) / len(durations) if durations else 0
            return {
                "total": total,
                "successful": successful,
                "failed": failed,
                "rolled_back": rolled_back,
                "success_rate": successful / total if total > 0 else 0.0,
                "avg_duration_ms": avg_duration,
            }


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
            from core_system.hot_update_service import HotUpdateService
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
        """Start automatic update checking."""
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
        the last-known set.  When a change is detected and the system is
        healthy, routes through the hot_reload_watcher (which prepares a
        standby generation and requests a handover).  This is a detection
        layer only — the actual update execution is governed by the
        decision-sovereign / synchronization-sovereign / boot_core chain.
        """
        while not self._stop_auto.is_set():
            try:
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
                    # Route through the hot_reload_watcher so the governed
                    # handover chain (prepare → request → boot_core handover)
                    # handles the actual update.
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
        src roots and compares against the last-known hash set.  Returns
        the set of module names whose source has changed.
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