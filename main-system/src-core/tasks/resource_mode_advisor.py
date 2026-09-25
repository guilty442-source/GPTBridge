"""Resource mode advisor — 需求驅動效能模式自動調整（§10.64 延伸）。

Registers the governed ``resource-mode-advisor`` automation flow onto the
AutomationCore (fail-closed allowlist; no private loop when the core
exists — same pattern as ``GitAutomationService``).

The tick itself is deliberately thin: the control law lives in
``tasks.resource_governor_signal.auto_adjust_mode`` so the signal/state
knowledge stays in one module.  The advisor is inert unless the rules
file carries ``auto_mode: true`` (預設自動) — a manual mode selection
always wins.  When ``power_saving_schedule.enabled`` the advisor forces
``sleep`` (睡眠/省電深眠 CPU 5% strict / RAM 30% / VRAM disabled) during
the nightly window 22:00-07:00; the schedule is resolved from
``resource-governor-rules.json`` with defaults enabled (sleep/low/medium/high).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

_logger = logging.getLogger("gptbridge.resource_mode_advisor")

FLOW_ID = "resource-mode-advisor"
DEFAULT_INTERVAL_S = 60.0


class ResourceModeAdvisor:
    """Periodic demand-driven governor-mode evaluation."""

    def __init__(
        self,
        project_root: str | Path | None = None,
        *,
        scheduler: Any | None = None,
        automation_core: Any | None = None,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self.project_root = Path(project_root) if project_root else None
        self._scheduler = scheduler
        self._automation_core = automation_core
        self.interval_s = float(interval_s)
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()

    async def start(self) -> dict[str, Any]:
        if self._automation_core is not None:
            if self._automation_core.register_flow(
                FLOW_ID,
                self._tick,
                interval_s=self.interval_s,
                run_immediately=True,
            ):
                _logger.info(
                    "resource mode advisor started via automation core "
                    "(interval=%.0fs)",
                    self.interval_s,
                )
                return {"status": "started", "loop": "automation-core"}
            _logger.info("resource mode advisor disabled by automation core")
            return {"status": "disabled", "loop": "automation-core"}
        if self._scheduler is not None:
            self._scheduler.register(
                FLOW_ID, self.interval_s, self._tick,
                run_immediately=True, pausable=False,
            )
            _logger.info(
                "resource mode advisor started on periodic scheduler "
                "(interval=%.0fs)",
                self.interval_s,
            )
            return {"status": "started", "loop": "periodic-scheduler"}
        try:
            self._task = asyncio.create_task(self._loop(), name=FLOW_ID)
        except RuntimeError:
            self._task = None
            return {"status": "no_event_loop"}
        _logger.info(
            "resource mode advisor started (interval=%.0fs)", self.interval_s
        )
        return {"status": "started"}

    async def _tick(self) -> None:
        from tasks.resource_governor_signal import auto_adjust_mode

        await asyncio.to_thread(auto_adjust_mode)

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("resource mode advisor tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.interval_s
                )
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop_event.set()
        if self._automation_core is not None:
            self._automation_core.unregister(FLOW_ID)
        elif self._scheduler is not None:
            self._scheduler.unregister(FLOW_ID)
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


__all__ = ["FLOW_ID", "ResourceModeAdvisor"]
