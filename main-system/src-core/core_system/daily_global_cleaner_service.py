"""Daily global cleaner service — facade.

This module provides the DailyGlobalCleanerService class.  The module
self-cleanup sweep lives in
:mod:`core_system.daily_global_cleaner_service_sweep`.

Maintenance-owned daily trigger for the governed Global Cleaner.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .daily_global_cleaner_service_sweep import DailyGlobalCleanerSweepMixin
from .daily_global_cleaner_service_run import DailyGlobalCleanerRunMixin


class DailyGlobalCleanerService(DailyGlobalCleanerSweepMixin, DailyGlobalCleanerRunMixin):
    """Maintenance-owned daily trigger for the governed Global Cleaner."""

    INTERVAL_SECONDS = 24 * 60 * 60
    FAILURE_RETRY_SECONDS = 15 * 60
    STARTUP_DELAY_SECONDS = 60
    RESPONSE_TIMEOUT_SECONDS = 30 * 60
    MODULE_CLEANUP_COMMAND = "toolbox_run_local_cleanup"
    MODULE_CLEANUP_TIMEOUT_SECONDS = 90
    STALE_RECORD_SECONDS = 2 * 24 * 60 * 60
    IN_PROCESS_MODULE_IDS = ("main-system",)
    EXCLUDED_MODULE_IDS = frozenset({"governance_rule"})

    def __init__(self, app: Any) -> None:
        self.app = app
        self.state_path = (
            Path(app.project_root)
            / "main-system"
            / "runtime"
            / "state"
            / "daily-global-cleaner.json"
        )
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None
        self._run_lock = asyncio.Lock()

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _permission_master_entry(self) -> Any:
        permission = getattr(self.app, "permission_sovereign", None)
        if permission is not None:
            return permission
        decision_sovereign = getattr(self.app, "decision_sovereign", None)
        if decision_sovereign is not None:
            return getattr(decision_sovereign, "permission_sovereign", None)
        return None

    def _load_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}

    def _save_state(self, payload: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)

    @staticmethod
    def _parse_iso_epoch(value: object) -> float:
        try:
            text = str(value or "").strip()
            if not text:
                return 0.0
            return datetime.fromisoformat(
                text.replace("Z", "+00:00")
            ).timestamp()
        except (TypeError, ValueError, OSError):
            return 0.0

    def _runtime_ready(self) -> bool:
        """Cleanup judgment gate: do not delete anything while the runtime
        reports not-ready (startup in progress, degraded, or unknown)."""

        readiness_path = self.state_path.parent / "runtime-readiness.json"
        try:
            payload = json.loads(readiness_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        snapshot = payload.get("snapshot")
        if isinstance(snapshot, dict):
            payload = snapshot
        return payload.get("overall_ready") is True

    @staticmethod
    def _valid_epoch(value: object) -> float:
        try:
            epoch = float(value or 0)
        except (TypeError, ValueError):
            return 0.0
        return epoch if math.isfinite(epoch) and epoch >= 0 else 0.0

    def _next_due_epoch(self, state: dict[str, Any]) -> float:
        last_started = self._valid_epoch(state.get("last_started_epoch"))
        delay = (
            self.INTERVAL_SECONDS
            if state.get("last_ok") is True
            else self.FAILURE_RETRY_SECONDS
        )
        return last_started + delay

    def status(self) -> dict[str, Any]:
        state = self._load_state()
        return {
            "enabled": True,
            "owner": "health-maintenance-test-sub-sovereign",
            "executor": "global-cleaner",
            "channel": "governance-authenticated-shared-layer",
            "interval_hours": 24,
            "last_started_at": state.get("last_started_at", ""),
            "last_completed_at": state.get("last_completed_at", ""),
            "last_ok": state.get("last_ok"),
            "failure_retry_minutes": self.FAILURE_RETRY_SECONDS // 60,
            "next_due_epoch": self._next_due_epoch(state),
            "module_cleanup": state.get("module_cleanup"),
        }

    def module_cleanup_status(self) -> dict[str, Any]:
        """Latest module self-cleanup sweep aggregated by the scheduler."""

        state = self._load_state()
        report = state.get("module_cleanup")
        return dict(report) if isinstance(report, dict) else {}

    def is_due(self, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        return current >= self._next_due_epoch(self._load_state())

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(
                self._run_loop(),
                name="daily-global-cleaner",
            )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run_loop(self) -> None:
        try:
            await asyncio.wait_for(
                self._stop_event.wait(),
                timeout=self.STARTUP_DELAY_SECONDS,
            )
            return
        except asyncio.TimeoutError:
            pass
        while not self._stop_event.is_set():
            try:
                if self.is_due():
                    await self.run_if_due()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                state = self._load_state()
                state.update(
                    {
                        "last_ok": False,
                        "last_status": "unexpected_failure",
                        "last_completed_at": self._iso_now(),
                        "last_error": {
                            "error_code": "GLOBAL_CLEANER_UNEXPECTED_FAILURE",
                            "message": f"{type(error).__name__}: {error}",
                        },
                    }
                )
                try:
                    self._save_state(state)
                except OSError:
                    pass
            wait_seconds = max(
                1.0,
                min(
                    60 * 60,
                    self._next_due_epoch(self._load_state()) - time.time(),
                ),
            )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=wait_seconds,
                )
            except asyncio.TimeoutError:
                continue


__all__ = ["DailyGlobalCleanerService"]
