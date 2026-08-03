from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any, Callable, Awaitable


@dataclasses.dataclass
class TaskRecord:
    command: str
    payload: dict[str, Any]
    category: str = "core"
    stage: str = "queued"
    phase: str = "queued"
    percent: int = 0
    status: str = "running"
    message: str = ""
    created_at: float = dataclasses.field(default_factory=time.time)


class TaskQueue:
    """Lightweight queue used by IPC server progress hooks."""

    def __init__(self, project_root: Path, core_logger: Any | None = None) -> None:
        self.project_root = project_root.resolve()
        self.core_logger = core_logger
        self._recovery_required = False

    def pending_recovery(self) -> list[dict[str, Any]]:
        if not self._recovery_required:
            return []
        return [{"id": "pending-task", "message": "unfinished task detected"}]

    def resolve_recovery(self, resume: bool) -> dict[str, Any]:
        self._recovery_required = False
        return {"ok": True, "resume": resume, "task_count": 0}

    async def begin(
        self,
        command: str,
        payload: dict[str, Any],
        send_event: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> TaskRecord:
        record = TaskRecord(command=command, payload=payload)
        await send_event(
            "task_progress",
            {
                "command": command,
                "stage": record.stage,
                "phase": record.phase,
                "percent": record.percent,
                "status": record.status,
            },
        )
        return record

    async def update_progress(
        self,
        task_record: TaskRecord | None,
        send_event: Callable[[str, dict[str, Any]], Awaitable[None]],
        *,
        stage: str | None = None,
        phase: str | None = None,
        percent: int | None = None,
        message: str | None = None,
    ) -> None:
        if task_record is None:
            return
        if stage is not None:
            task_record.stage = stage
        if phase is not None:
            task_record.phase = phase
        if percent is not None:
            task_record.percent = max(0, min(100, int(percent)))
        if message is not None:
            task_record.message = message

        await send_event(
            "task_progress",
            {
                "command": task_record.command,
                "stage": task_record.stage,
                "phase": task_record.phase,
                "percent": task_record.percent,
                "status": task_record.status,
                "message": task_record.message,
            },
        )

    async def finish(
        self,
        task_record: TaskRecord | None,
        ok: bool,
        send_event: Callable[[str, dict[str, Any]], Awaitable[None]],
        payload: dict[str, Any],
    ) -> None:
        if task_record is None:
            return
        task_record.status = "completed" if ok else "failed"
        task_record.percent = 100 if ok else task_record.percent
        await send_event(
            "task_finished",
            {
                "command": task_record.command,
                "status": task_record.status,
                "percent": task_record.percent,
                "payload": payload,
            },
        )

    async def cancel(
        self,
        task_record: TaskRecord | None,
        send_event: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        if task_record is None:
            return
        task_record.status = "cancelled"
        await send_event(
            "task_cancelled",
            {
                "command": task_record.command,
                "status": task_record.status,
            },
        )

