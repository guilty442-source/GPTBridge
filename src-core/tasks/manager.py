from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any


TaskHandler = Callable[[dict[str, Any]], Any]


class TaskManager:
    """Small in-memory task registry used by lightweight core workflows."""

    def __init__(self) -> None:
        self._tasks: dict[str, dict[str, Any]] = {}
        self._handlers: dict[str, TaskHandler] = {}

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        key = str(task_type).strip()
        if not key:
            raise ValueError("task_type is required")
        self._handlers[key] = handler

    def create_task(
        self,
        task_type: str,
        payload: dict[str, Any] | None = None,
        *,
        title: str = "",
    ) -> dict[str, Any]:
        key = str(task_type).strip()
        if not key:
            raise ValueError("task_type is required")
        now = time.time()
        task = {
            "task_id": uuid.uuid4().hex[:16],
            "task_type": key,
            "title": title or key,
            "payload": dict(payload or {}),
            "status": "pending",
            "created_at": now,
            "updated_at": now,
            "result": None,
            "error": "",
        }
        self._tasks[str(task["task_id"])] = task
        return dict(task)

    def list_tasks(self, status: str = "") -> list[dict[str, Any]]:
        wanted = status.strip().lower()
        tasks = self._tasks.values()
        if wanted:
            tasks = [task for task in tasks if str(task.get("status", "")).lower() == wanted]
        return [dict(task) for task in sorted(tasks, key=lambda item: float(item["created_at"]))]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(str(task_id))
        return dict(task) if task else None

    def update_task(self, task_id: str, **updates: Any) -> dict[str, Any]:
        task = self._tasks.get(str(task_id))
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        task.update(updates)
        task["updated_at"] = time.time()
        return dict(task)

    def run_task(self, task_id: str) -> dict[str, Any]:
        task = self._tasks.get(str(task_id))
        if task is None:
            raise KeyError(f"task not found: {task_id}")
        handler = self._handlers.get(str(task.get("task_type")))
        if handler is None:
            return self.update_task(task_id, status="failed", error="task handler is not registered")
        self.update_task(task_id, status="running", error="")
        try:
            result = handler(dict(task.get("payload") or {}))
        except Exception as exc:
            return self.update_task(task_id, status="failed", error=str(exc))
        return self.update_task(task_id, status="completed", result=result)

    def run_pending(self) -> list[dict[str, Any]]:
        return [self.run_task(str(task["task_id"])) for task in self.list_tasks("pending")]
