"""Hot-reload watcher health monitoring mixin.

Provides channel health monitoring and auto-recovery for the
HotReloadWatcher class.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from core_system.sovereign_utils import _iso_now

from .hot_reload_watcher_constants import (
    HEALTH_CHECK_INTERVAL_SECONDS,
    MAX_CONSECUTIVE_FAILURES,
)


class HotReloadHealthMixin:
    """Channel health monitoring and recovery methods for HotReloadWatcher."""

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

        # Check automation sovereign
        automation_sovereign = getattr(app, "automation_sovereign", None)
        if automation_sovereign is None:
            healthy = False
            errors.append("Automation sovereign: missing")

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
