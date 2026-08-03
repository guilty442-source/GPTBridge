from __future__ import annotations

import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DailyGlobalCleanerService:
    """Main-owned daily trigger; execution remains with governed Global Cleaner."""

    INTERVAL_SECONDS = 24 * 60 * 60
    FAILURE_RETRY_SECONDS = 15 * 60
    STARTUP_DELAY_SECONDS = 60
    RESPONSE_TIMEOUT_SECONDS = 30 * 60

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
            "owner": "main-system",
            "executor": "global-cleaner",
            "channel": "governance-authenticated-shared-layer",
            "interval_hours": 24,
            "last_started_at": state.get("last_started_at", ""),
            "last_completed_at": state.get("last_completed_at", ""),
            "last_ok": state.get("last_ok"),
            "failure_retry_minutes": self.FAILURE_RETRY_SECONDS // 60,
            "next_due_epoch": self._next_due_epoch(state),
        }

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

    async def run_if_due(self, *, force: bool = False) -> dict[str, Any]:
        async with self._run_lock:
            if not force and not self.is_due():
                return {"ok": True, "skipped": True, "reason": "NOT_DUE"}
            toolbox = self.app.toolbox_service
            governance = self.app.governance
            if toolbox is None or governance is None:
                return {
                    "ok": False,
                    "error_code": "GOVERNED_RUNTIME_UNAVAILABLE",
                }
            request_id = f"daily-global-cleaner-{time.time_ns()}"
            state = self._load_state()
            state.update(
                {
                    "last_started_epoch": time.time(),
                    "last_started_at": self._iso_now(),
                    "last_request_id": request_id,
                    "last_ok": False,
                    "last_status": "starting",
                }
            )
            state.pop("last_error", None)
            self._save_state(state)
            try:
                start_result = await toolbox.start_tool(
                    {
                        "tool_id": "global-cleaner",
                        "request_id": f"start-{request_id}",
                        "background": True,
                        "runtime_mode": "source",
                    }
                )
            except Exception as error:
                start_result = {
                    "ok": False,
                    "error_code": "GLOBAL_CLEANER_START_EXCEPTION",
                    "message": f"{type(error).__name__}: {error}",
                }
            if start_result.get("ok") is not True:
                state.update(
                    {
                        "last_status": "start_failed",
                        "last_completed_at": self._iso_now(),
                        "last_error": start_result,
                    }
                )
                self._save_state(state)
                return {"ok": False, "stage": "start", "detail": start_result}
            started_here = "already running" not in str(
                start_result.get("message") or ""
            ).casefold()
            response: dict[str, Any] | None = None
            try:
                queued = await toolbox.request_tool_execution(
                    {
                        "tool_id": "global-cleaner",
                        "request_id": request_id,
                        "_governed_command": "toolbox_request_tool_execution",
                        "args": ["--governed-daily-maintenance", "--json"],
                    }
                )
                if queued.get("ok") is not True:
                    return {"ok": False, "stage": "queue", "detail": queued}
                deadline = time.monotonic() + self.RESPONSE_TIMEOUT_SECONDS
                while time.monotonic() < deadline:
                    response = await asyncio.to_thread(
                        governance.tool_execution_response,
                        "global-cleaner",
                        request_id,
                    )
                    if response and response.get("status") in {
                        "completed",
                        "failed",
                        "cancelled",
                    }:
                        break
                    await asyncio.sleep(1)
                if not response or response.get("status") != "completed":
                    return {
                        "ok": False,
                        "stage": "execute",
                        "error_code": "GLOBAL_CLEANER_TIMEOUT",
                        "detail": response,
                    }
                tool_response = response.get("response")
                ok = isinstance(tool_response, dict) and tool_response.get("ok") is True
                return {
                    "ok": ok,
                    "stage": "completed",
                    "request_id": request_id,
                    "detail": tool_response,
                }
            finally:
                close_result = (
                    await toolbox.force_close_tool(
                        {
                            "tool_id": "global-cleaner",
                            "request_id": f"stop-{request_id}",
                        }
                    )
                    if started_here
                    else {
                        "ok": True,
                        "delegated": True,
                        "reason": "PREEXISTING_GLOBAL_CLEANER_RETAINED",
                    }
                )
                result_ok = bool(
                    response
                    and response.get("status") == "completed"
                    and isinstance(response.get("response"), dict)
                    and response["response"].get("ok") is True
                )
                state.update(
                    {
                        "last_completed_at": self._iso_now(),
                        "last_ok": result_ok,
                        "last_status": "completed" if result_ok else "failed",
                        "last_response": response,
                        "last_close_result": close_result,
                    }
                )
                self._save_state(state)
