"""Automatic cleanup service — facade.

This module provides the DailyGlobalCleanerService class.  The module
self-cleanup sweep lives in
:mod:`core_system.daily_global_cleaner_service_sweep`.

A533/A534: the standalone global-cleaner tool is retired and
non-executable.  Automatic cleanup is owned in-process by the
health-maintenance-test-sub-sovereign through this main-system internal
service (bounded temp/cache/expired-log/orphan residue cleanup, retired
trash residue revalidation, central audit events, fail-open execution).
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
    """Maintenance-owned scheduling of the main-system internal cleanup."""

    INTERVAL_SECONDS = 24 * 60 * 60
    FAILURE_RETRY_SECONDS = 15 * 60
    STARTUP_DELAY_SECONDS = 60
    MODULE_CLEANUP_COMMAND = "toolbox_run_local_cleanup"
    MODULE_CLEANUP_TIMEOUT_SECONDS = 90
    STALE_RECORD_SECONDS = 2 * 24 * 60 * 60
    IN_PROCESS_MODULE_IDS = ("main-system",)
    EXCLUDED_MODULE_IDS = frozenset({"governance_rule"})
    # Bounded cycle budget: never clean more than 1 GiB per daily cycle and
    # never sweep more than a fixed number of retired trash roots.
    CYCLE_BYTE_QUOTA = 1024 * 1024 * 1024
    MAX_MODULES_PER_CYCLE = 16
    TRASH_MAX_MODULES_PER_CYCLE = 4
    ARCHITECTURE_REGISTRY_PATH = (
        "governance_rule/execution/audit/architecture_registry.json"
    )
    TRASH_SCHEDULE_NAME = "trash-cleanup-schedule.json"

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
        trash_schedule: dict[str, Any] = {}
        try:
            loaded = json.loads(
                (self.state_path.parent / self.TRASH_SCHEDULE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            if isinstance(loaded, dict):
                trash_schedule = loaded
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass
        return {
            "enabled": True,
            "owner": "health-maintenance-test-sub-sovereign",
            "executor": "main-system-internal-cleanup",
            "channel": "in-process-bounded-cleanup",
            "interval_hours": 24,
            "last_started_at": state.get("last_started_at", ""),
            "last_completed_at": state.get("last_completed_at", ""),
            "last_ok": state.get("last_ok"),
            "failure_retry_minutes": self.FAILURE_RETRY_SECONDS // 60,
            "next_due_epoch": self._next_due_epoch(state),
            "quota": {
                "cycle_bytes": self.CYCLE_BYTE_QUOTA,
                "max_modules_per_cycle": self.MAX_MODULES_PER_CYCLE,
                "trash_max_modules_per_cycle": self.TRASH_MAX_MODULES_PER_CYCLE,
            },
            "module_cleanup": state.get("module_cleanup"),
            "last_outcome": state.get("last_outcome"),
            "quarantine_retention": state.get("quarantine_retention"),
            "orphan_classification": state.get("orphan_classification"),
            "trash_cleanup_schedule": trash_schedule,
        }

    def module_cleanup_status(self) -> dict[str, Any]:
        """Latest module self-cleanup sweep aggregated by the scheduler."""

        state = self._load_state()
        report = state.get("module_cleanup")
        return dict(report) if isinstance(report, dict) else {}

    def _trash_candidates(self) -> list[dict[str, Any]]:
        """Registry-classified retired trash roots, revalidated per cycle.

        The inspection schedule is advisory: only component ids that are
        still classified ``trash`` + ``retired`` in the *current*
        architecture registry are returned, so the schedule can never
        resurrect or widen the deletion scope by itself.
        """
        try:
            from tasks.repair_inspection import classify_trash_modules
        except Exception:
            return []
        try:
            classified = classify_trash_modules(Path(self.app.project_root))
        except Exception:
            return []
        scheduled: set[str] = set()
        try:
            payload = json.loads(
                (self.state_path.parent / self.TRASH_SCHEDULE_NAME).read_text(
                    encoding="utf-8"
                )
            )
            candidate_ids = (
                payload.get("candidate_ids")
                if isinstance(payload, dict)
                else None
            )
            if isinstance(candidate_ids, list):
                scheduled = {
                    str(item).strip()
                    for item in candidate_ids
                    if str(item).strip()
                }
        except (OSError, UnicodeError, json.JSONDecodeError):
            scheduled = set()
        if not scheduled:
            return classified
        return [
            item
            for item in classified
            if str(item.get("component_id") or "") in scheduled
        ]

    def _record_cleanup_audit(self, outcome: dict[str, Any]) -> str:
        """Append one bounded central-audit event; never raises (fail-open)."""

        try:
            from .audit_integration import build_audit_adapter

            adapter = build_audit_adapter(self.app, fail_open=True)
            return adapter.record_event(
                action="automatic-cleanup",
                outcome=(
                    "success"
                    if bool(outcome.get("findings", {}).get("modules", {}).get("ok"))
                    and outcome.get("findings", {}).get("trash", {}).get("ok")
                    is not False
                    else "failure"
                ),
                actor_id="governance/main-system",
                resource_id="main-system-internal-cleanup",
                correlation_id=str(outcome.get("request_id") or ""),
                details={
                    "scope": ",".join(outcome.get("scope") or ()),
                    "cleaned_bytes": int(outcome.get("cleaned_bytes") or 0),
                    "duration_seconds": outcome.get("duration_seconds", 0),
                    "byte_quota": int(outcome.get("byte_quota") or 0),
                },
            )
        except Exception:
            return "unavailable"

    def is_due(self, now: float | None = None) -> bool:
        current = time.time() if now is None else float(now)
        return current >= self._next_due_epoch(self._load_state())

    async def start(self) -> None:
        scheduler = getattr(self.app, "periodic_scheduler", None)
        if scheduler is not None:
            # §10.63 R3: shared loop; the job re-checks is_due at a coarse
            # cadence — due semantics unchanged (run_if_due only fires at
            # the computed deadline).
            scheduler.register(
                "daily-global-cleaner",
                300.0,
                self._scheduled_tick,
            )
            return
        if self._task is None or self._task.done():
            self._stop_event.clear()
            self._task = asyncio.create_task(
                self._run_loop(),
                name="daily-global-cleaner",
            )

    async def stop(self) -> None:
        self._stop_event.set()
        scheduler = getattr(self.app, "periodic_scheduler", None)
        if scheduler is not None:
            scheduler.unregister("daily-global-cleaner")
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _scheduled_tick(self) -> None:
        """One due-check for the shared scheduler (same body as _run_loop)."""
        if self._stop_event.is_set():
            return
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
