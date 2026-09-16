"""Maintenance scheduler.

VACUUM, ANALYZE, REINDEX, backup and integrity checks never all run at the
same time: one heavy task at a time, only while the system is idle enough,
each with its own minimum interval.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Final

from .types import AdaptiveEnvelope, LoadSignals, PressureLevel


@dataclass(frozen=True)
class MaintenanceTask:
    name: str
    interval_seconds: float
    weight: int


DEFAULT_TASKS: Final[tuple[MaintenanceTask, ...]] = (
    MaintenanceTask("vacuum", 900.0, 3),
    MaintenanceTask("analyze", 1800.0, 1),
    MaintenanceTask("reindex", 21600.0, 4),
    MaintenanceTask("backup", 21600.0, 2),
    MaintenanceTask("integrity_check", 86400.0, 2),
)


@dataclass
class _TaskState:
    last_run: float = field(default=0.0)


class MaintenanceScheduler:
    """Idle-aware, single-task-at-a-time maintenance planner."""

    def __init__(
        self,
        tasks: tuple[MaintenanceTask, ...] = DEFAULT_TASKS,
        envelope: AdaptiveEnvelope | None = None,
    ) -> None:
        self.tasks = tasks
        self.envelope = envelope or AdaptiveEnvelope()
        self._lock = threading.RLock()
        self._state: dict[str, _TaskState] = {
            task.name: _TaskState(last_run=time.monotonic()) for task in tasks
        }
        self._running: str | None = None

    def idle_enough(self, signals: LoadSignals) -> tuple[bool, str]:
        if signals.degraded:
            return False, "degraded-mode"
        if signals.pressure() not in (PressureLevel.LOW,):
            return False, "pressure"
        if signals.transport_backlog >= self.envelope.transport_backlog_moderate:
            return False, "transport-backlog"
        if signals.reconcile_backlog >= self.envelope.transport_backlog_moderate:
            return False, "reconcile-backlog"
        if signals.model_load_pct >= self.envelope.cpu_high_pct:
            return False, "model-load"
        return True, "idle"

    def next_task(
        self,
        signals: LoadSignals,
        *,
        now: float | None = None,
    ) -> MaintenanceTask | None:
        moment = time.monotonic() if now is None else now
        ok, _reason = self.idle_enough(signals)
        if not ok:
            return None
        with self._lock:
            if self._running is not None:
                return None
            due: list[MaintenanceTask] = []
            for task in self.tasks:
                state = self._state[task.name]
                if moment - state.last_run >= task.interval_seconds:
                    due.append(task)
            if not due:
                return None
            # Heavier tasks first, but only one task per decision.
            chosen = sorted(due, key=lambda task: (-task.weight, task.name))[0]
            return chosen

    def mark_running(self, task: MaintenanceTask) -> None:
        with self._lock:
            self._running = task.name

    def mark_done(
        self,
        task: MaintenanceTask,
        *,
        now: float | None = None,
    ) -> None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._state[task.name].last_run = moment
            if self._running == task.name:
                self._running = None

    def running(self) -> str | None:
        return self._running

    def snapshot(self, *, now: float | None = None) -> dict[str, object]:
        moment = time.monotonic() if now is None else now
        with self._lock:
            return {
                "running": self._running,
                "tasks": {
                    name: {
                        "interval_seconds": next(
                            task.interval_seconds for task in self.tasks if task.name == name
                        ),
                        "seconds_since_run": round(moment - state.last_run, 3),
                    }
                    for name, state in self._state.items()
                },
            }


__all__ = ["DEFAULT_TASKS", "MaintenanceScheduler", "MaintenanceTask"]
