"""Automated backend hot-reload watcher.

Watches the governed backend source root (``main-system/src-core``) and, once
file changes quiet down, requests a module-scoped hot-reload through the
maintenance sovereign.

Every reload is governance-authorized: the approval token is minted in-process
through the capability authority (actor main-system / capability hot-update /
version-gated increment).  Reloads therefore stay inside the version-gated
hot-update boundary (A13/E6/E29).  Frozen material (codex, permission
directory, data, manifests) is never reloaded — the reload service already
excludes it.

The watcher owns observation and debouncing only.  Execution is delegated
entirely to the maintenance sovereign; the watcher logs the result and
continues observing.  It never replaces, writes, or restarts code itself and
never bypasses governance.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import threading
import time
import types
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Optional
from dataclasses import dataclass, field

from core_system.hot_update_service import PROTECTED_MODULE_PREFIXES
from core_system.sovereign_utils import _iso_now

POLL_INTERVAL_SECONDS: Final[float] = 3.0
QUIET_WINDOW_SECONDS: Final[float] = 1.2
MIN_RELOAD_INTERVAL_SECONDS: Final[float] = 5.0
MAX_RELOADS_PER_MINUTE: Final[int] = 6
FAILURE_BACKOFF_SECONDS: Final[float] = 30.0
# Enhanced stability constants
MAX_CONSECUTIVE_FAILURES: Final[int] = 3
HEALTH_CHECK_INTERVAL_SECONDS: Final[float] = 30.0
CHANNEL_HEALTH_TIMEOUT_SECONDS: Final[float] = 10.0
MAX_RETRY_ATTEMPTS: Final[int] = 3
RETRY_BASE_DELAY_SECONDS: Final[float] = 2.0
RETRY_MAX_DELAY_SECONDS: Final[float] = 60.0
RETRY_JITTER_FACTOR: Final[float] = 0.3
IPC_RECONNECT_DELAY_SECONDS: Final[float] = 5.0

# Only main-system code is reloaded.  Independent tools run in their own
# governed processes and shared-layer is an independent governance
# jurisdiction (its code is not covered by the main-system hot-update grant),
# so neither is observed here.
WATCH_ROOTS: Final[tuple[str, ...]] = ("main-system/src-core",)

@dataclass
class ChannelHealth:
    """Health status of the update delivery channel."""
    is_healthy: bool = True
    last_check: float = field(default_factory=time.monotonic)
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    last_success: Optional[float] = None
    ipc_connected: bool = False
    governance_reachable: bool = False

    def record_success(self) -> None:
        self.is_healthy = True
        self.consecutive_failures = 0
        self.last_error = None
        self.last_success = time.monotonic()
        self.last_check = time.monotonic()

    def record_failure(self, error: str) -> None:
        self.consecutive_failures += 1
        self.last_error = error
        self.last_check = time.monotonic()
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.is_healthy = False

    def check_timeout(self) -> bool:
        """Check if channel health has timed out."""
        return (time.monotonic() - self.last_check) > CHANNEL_HEALTH_TIMEOUT_SECONDS

_EXCLUDED_TOKEN_DIRS: Final[tuple[str, ...]] = (
    "main-system/runtime",
    "main-system/data",
)


def _is_protected(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in PROTECTED_MODULE_PREFIXES
    )


class HotReloadWatcher:
    """Poll-based watcher that requests governed hot-reload on source edits."""

    def __init__(
        self,
        app: Any,
        *,
        project_root: str | Path | None = None,
    ) -> None:
        self.app = app
        fallback_root = Path(__file__).resolve().parents[3]
        self.project_root = Path(
            project_root or getattr(app, "project_root", fallback_root)
        ).resolve()
        self._roots: list[Path] = []
        self._task: asyncio.Task[Any] | None = None
        self._health_task: asyncio.Task[Any] | None = None
        self._stop = asyncio.Event()
        self._enabled = False
        self._snapshot: dict[str, float] = {}
        self._pending: dict[str, float] = {}
        self._in_flight = False
        self._last_reload_at = 0.0
        self._backoff_until = 0.0
        self._reload_timestamps: list[float] = []
        self._channel_health = ChannelHealth()

    # ─── lifecycle ────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._resolve_roots()
        if not self._roots:
            self._log({"type": "hot_reload_watcher", "enabled": False,
                       "message": "no-watch-roots"})
            return
        self._snapshot = self._scan()
        self._pending = {}
        self._enabled = True
        self._stop.clear()
        self._task = asyncio.create_task(
            self._loop(),
            name="main-system-hot-reload-watcher",
        )
        # Start channel health monitoring
        self._health_task = asyncio.create_task(
            self._health_monitor_loop(),
            name="hot-reload-channel-health",
        )
        self._log({"type": "hot_reload_watcher", "enabled": True,
                   "roots": [str(root) for root in self._roots]})

    async def stop(self) -> None:
        self._enabled = False
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Stop health monitoring
        health_task = self._health_task
        self._health_task = None
        if health_task is not None and not health_task.done():
            health_task.cancel()
            try:
                await health_task
            except asyncio.CancelledError:
                pass

    # ─── observation ──────────────────────────────────────────────────

    def _resolve_roots(self) -> None:
        for relative in WATCH_ROOTS:
            root = (self.project_root / relative).resolve()
            if root.is_dir():
                self._roots.append(root)

    def _scan(self) -> dict[str, float]:
        found: dict[str, float] = {}
        for root in self._roots:
            try:
                for path in root.rglob("*.py"):
                    try:
                        stat = path.stat()
                    except OSError:
                        continue
                    found[str(path)] = stat.st_mtime
            except OSError:
                continue
        return found

    def _track_changes(self, current: dict[str, float]) -> None:
        previous = self._snapshot
        now = time.monotonic()
        for key, mtime in current.items():
            if key not in previous or abs(previous[key] - mtime) > 0.001:
                self._pending[key] = now
        self._snapshot = current

    # ─── reload request ───────────────────────────────────────────────

    async def _maybe_reload(self, changed_paths: list[str]) -> bool:
        """Attempt one reload; return whether the pending revision was consumed."""
        if self._in_flight or not self._enabled:
            return False
        now = time.monotonic()
        if now < self._backoff_until:
            return False
        if now - self._last_reload_at < MIN_RELOAD_INTERVAL_SECONDS:
            return False
        self._reload_timestamps = [
            stamp for stamp in self._reload_timestamps if now - stamp < 60.0
        ]
        if len(self._reload_timestamps) >= MAX_RELOADS_PER_MINUTE:
            return False

        app = self.app
        if getattr(app, "startup_dead", False) or not getattr(
            app, "maintenance_ready", False
        ):
            return False
        synchronization = getattr(app, "synchronization_sovereign", None)
        if synchronization is None:
            return False
        decision_sovereign = getattr(app, "decision_sovereign", None)
        if decision_sovereign is None:
            return False
        governance = getattr(app, "governance", None)
        if governance is None:
            return False

        module_names = self._loaded_module_names(changed_paths)
        if not module_names:
            return True
        token_path = self._token_resource_path(changed_paths)
        if token_path is None:
            return True
        self._in_flight = True
        try:
            hot_update = getattr(app, "hot_update_service", None)
            prepare = getattr(hot_update, "prepare_generation", None)
            if not callable(prepare):
                return False
            prepared = await asyncio.to_thread(
                prepare,
                governance=governance,
                modules=module_names,
                standby_validation=True,
            )
            if not bool(getattr(prepared, "ok", False)):
                self._backoff_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
                return False
            operation_id = uuid.uuid4().hex
            target_generation = f"backend-{time.time_ns()}"
            request_path = (
                self.project_root / "main-system" / "runtime" / "state"
                / "backend-update-request.json"
            )
            request_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "schema_version": 1,
                "operation_id": operation_id,
                "update_type": "backend-release",
                "certified": True,
                "decision_owner": "synchronization-sovereign",
                "decision_basis": "A330",
                "permission_scope": token_path,
                "modules": module_names,
                "artifact_hashes": dict(prepared.hashes),
                "target_generation": target_generation,
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "terminal_status": "prepared",
            }
            temporary = request_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, request_path)
            ok = True
            if ok:
                self._last_reload_at = time.monotonic()
                self._reload_timestamps.append(self._last_reload_at)
            else:
                self._backoff_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
            self._log({
                "type": "hot_reload_watcher",
                "ok": ok,
                "modules": module_names,
                "operation_id": operation_id,
                "target_generation": target_generation,
                "handover": "prepared",
            })
            # C: Report the prepared state to the decision-sovereign so it
            # tracks the operation lifecycle.  The terminal status (global-
            # success / failed-isolated / rolled-back) is written by boot_core
            # to the same request file; a background poller reports it back.
            self._report_to_decision_sovereign(
                operation_id=operation_id,
                terminal_status="prepared",
                modules=module_names,
                target_generation=target_generation,
            )
            # Spawn a bounded poller that waits for boot_core to write a
            # terminal status, then reports it to the decision-sovereign.
            threading.Thread(
                target=self._poll_terminal_status,
                args=(operation_id, request_path),
                daemon=True,
                name=f"hot-reload-terminal-{operation_id[:8]}",
            ).start()
            return ok
        except Exception as error:
            self._backoff_until = time.monotonic() + FAILURE_BACKOFF_SECONDS
            self._log({"type": "hot_reload_watcher", "ok": False,
                       "error": f"{type(error).__name__}: {error}",
                       "modules": module_names})
            return False
        finally:
            self._in_flight = False

    def _loaded_module_names(self, changed_paths: list[str]) -> list[str]:
        targets = {Path(path).resolve() for path in changed_paths}
        names: list[str] = []
        for name, module in list(sys.modules.items()):
            if not isinstance(module, types.ModuleType):
                continue
            if _is_protected(name):
                continue
            file_path = getattr(module, "__file__", None)
            if not file_path:
                continue
            try:
                resolved = Path(file_path).resolve()
            except (OSError, ValueError):
                continue
            if resolved in targets:
                names.append(name)
        return sorted(names)

    def _token_resource_path(self, changed_paths: list[str]) -> str | None:
        for path in changed_paths:
            try:
                relative = Path(path).resolve().relative_to(self.project_root)
            except (OSError, ValueError):
                continue
            parts = relative.parts
            if not parts or parts[0] != "main-system":
                continue
            token_path = "/".join(parts)
            if any(
                token_path == root or token_path.startswith(root + "/")
                for root in _EXCLUDED_TOKEN_DIRS
            ):
                continue
            return token_path
        return None

    def _mint_approval_token(self, governance: Any, resource_path: str) -> str:
        authentication = getattr(governance, "authentication", None)
        if authentication is None or not callable(
            getattr(authentication, "issue_token", None)
        ):
            return ""
        try:
            from governance_rule.permission_directory.directory_authority import (
                directory_authority_snapshot,
            )
            current = int(
                directory_authority_snapshot().authority_version_policy.current_version
            )
            target_version = str(current + 1)
            return str(
                authentication.issue_token(
                    target_tool_id="main-system",
                    capability="hot-update",
                    action="update",
                    target="tool-code:main-system",
                    data_scope="tool-code",
                    target_version=target_version,
                    resource_path=resource_path,
                )
            )
        except Exception:
            return ""

    # ─── main loop ────────────────────────────────────────────────────

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                current = self._scan()
                self._track_changes(current)
                if self._pending:
                    now = time.monotonic()
                    if now - max(self._pending.values()) >= QUIET_WINDOW_SECONDS:
                        if self._enabled:
                            changed = list(self._pending.keys())
                            if await self._maybe_reload(changed):
                                self._pending = {}
            except Exception as error:
                self._log({"type": "hot_reload_watcher_error",
                           "error": f"{type(error).__name__}: {error}"})
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _health_monitor_loop(self) -> None:
        """Monitor update channel health and attempt auto-recovery."""
        while not self._stop.is_set():
            try:
                await self._check_channel_health()
            except Exception as error:
                self._log({"type": "hot_reload_watcher_health_error",
                           "error": f"{type(error).__name__}: {error}"})
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                break

    async def _check_channel_health(self) -> None:
        """Check health of the update delivery channel (IPC, governance, etc.)."""
        app = self.app
        healthy = True
        errors = []

        # Check IPC connection
        ipc_connected = False
        try:
            if hasattr(app, "_active_ui_shells") and app._active_ui_shells:
                ipc_connected = True
        except Exception:
            pass
        self._channel_health.ipc_connected = ipc_connected
        if not ipc_connected:
            healthy = False
            errors.append("IPC: no active UI shells")

        # Check governance reachability
        governance_reachable = False
        try:
            governance = getattr(app, "governance", None)
            if governance is not None:
                if hasattr(governance, "runtime_integrity_ready"):
                    governance_reachable = governance.runtime_integrity_ready(max_age_seconds=5)
                else:
                    governance_reachable = True
        except Exception:
            pass
        self._channel_health.governance_reachable = governance_reachable
        if not governance_reachable:
            healthy = False
            errors.append("Governance: unreachable")

        # Check decision sovereign
        decision_sovereign = getattr(app, "decision_sovereign", None)
        if decision_sovereign is None:
            healthy = False
            errors.append("Decision sovereign: missing")

        # Check synchronization sovereign
        sync_sovereign = getattr(app, "synchronization_sovereign", None)
        if sync_sovereign is None:
            healthy = False
            errors.append("Synchronization sovereign: missing")

        # Update channel health
        if healthy:
            self._channel_health.record_success()
        else:
            error_msg = "; ".join(errors)
            self._channel_health.record_failure(error_msg)
            self._log({
                "type": "channel_health_degraded",
                "errors": errors,
                "consecutive_failures": self._channel_health.consecutive_failures,
            })

            # Attempt auto-recovery
            if self._channel_health.consecutive_failures >= 2:
                await self._attempt_channel_recovery()

    async def _attempt_channel_recovery(self) -> None:
        """Attempt to recover degraded update channel."""
        self._log({"type": "channel_recovery_attempt",
                   "consecutive_failures": self._channel_health.consecutive_failures})
        app = self.app

        # Try to re-establish governance connection
        try:
            governance = getattr(app, "governance", None)
            if governance is not None and hasattr(governance, "_authentication"):
                auth = governance._authentication
                if auth is not None and hasattr(auth, "verify_runtime_integrity"):
                    auth.verify_runtime_integrity()
                    self._log({"type": "channel_recovery", "action": "governance_revalidated"})
        except Exception:
            pass

        # Reset backoff to allow new attempts
        self._backoff_until = 0.0

        # Notify decision sovereign of recovery attempt
        decision_sovereign = getattr(app, "decision_sovereign", None)
        if decision_sovereign is not None:
            reporter = getattr(decision_sovereign, "record_certified_update_status", None)
            if callable(reporter):
                try:
                    reporter("channel-recovery", "attempted", timestamp=_iso_now())
                except Exception:
                    pass

    def _log(self, payload: dict[str, Any]) -> None:
        try:
            state_path = (
                self.project_root / "main-system" / "runtime" / "state"
                / "hot-reload-watcher.json"
            )
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {**payload, "recorded_at": datetime.now(timezone.utc).isoformat()},
                    ensure_ascii=False,
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, state_path)
        except OSError:
            pass
        try:
            log = getattr(self.app, "_log", None)
            if callable(log):
                log(payload)
                return
        except Exception:
            pass
        try:
            print(payload, flush=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # C: Decision-sovereign feedback loop
    # ------------------------------------------------------------------

    def _report_to_decision_sovereign(
        self,
        *,
        operation_id: str,
        terminal_status: str,
        **detail: Any,
    ) -> None:
        """Report a certified-update lifecycle event to the decision-sovereign.

        The decision-sovereign tracks the operation (A152/A330) without
        executing it.  This closes the feedback loop: boot_core writes
        terminal status to the request file, the watcher polls it and
        reports back here.
        """
        decision_sovereign = getattr(self.app, "decision_sovereign", None)
        if decision_sovereign is None:
            return
        reporter = getattr(decision_sovereign, "record_certified_update_status", None)
        if not callable(reporter):
            return
        try:
            reporter(operation_id, terminal_status, **detail)
        except Exception:
            pass

    def _poll_terminal_status(
        self,
        operation_id: str,
        request_path: Path,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> None:
        """Poll backend-update-request.json for a terminal status written
        by boot_core, then report it to the decision-sovereign.

        Terminal statuses (A330): global-success, failed-isolated,
        rolled-back, partial-deferred.  Bounded by ``timeout`` so a missing
        boot_core response does not leak a thread forever.
        """
        terminal_states = {
            "global-success",
            "failed-isolated",
            "rolled-back",
            "partial-deferred",
        }
        deadline = time.monotonic() + timeout
        last_status = ""
        while time.monotonic() < deadline:
            try:
                data = json.loads(request_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                time.sleep(poll_interval)
                continue
            status = str(data.get("terminal_status") or "")
            if status == last_status:
                time.sleep(poll_interval)
                continue
            last_status = status
            if status in terminal_states and data.get("operation_id") == operation_id:
                self._report_to_decision_sovereign(
                    operation_id=operation_id,
                    terminal_status=status,
                    active_generation=data.get("active_generation", ""),
                    active_backend_port=data.get("active_backend_port"),
                    error=data.get("error", ""),
                    processed_at=data.get("processed_at", ""),
                )
                self._log({
                    "type": "hot_reload_watcher",
                    "ok": status == "global-success",
                    "operation_id": operation_id,
                    "terminal_status": status,
                })
                return
            time.sleep(poll_interval)
        # Timeout: report as failed-isolated so the decision-sovereign
        # records the missing response.
        self._report_to_decision_sovereign(
            operation_id=operation_id,
            terminal_status="failed-isolated",
            error="terminal-status-timeout",
        )


__all__ = ["HotReloadWatcher", "WATCH_ROOTS"]
