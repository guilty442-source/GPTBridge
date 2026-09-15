from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any, Awaitable, Callable

from ..infrastructure.analytics_repository import InvestmentAnalyticsStore, parse_datetime, utc_now, utc_text
from ..infrastructure.privacy import protect_text, unprotect_text
from .automation_common import _json
from .automation_governance import ModelGovernance
from .automation_notifications import NotificationManager

__all__ = [
    "InvestmentAutomation",
    "ModelGovernance",
    "NotificationManager",
]


class InvestmentAutomation:
    def __init__(
        self,
        store: InvestmentAnalyticsStore,
        notifications: NotificationManager,
        cycle_callback: Callable[[], Awaitable[dict[str, Any]]],
        backup_callback: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.notifications = notifications
        self.cycle_callback = cycle_callback
        self.backup_callback = backup_callback
        self._task: asyncio.Task[Any] | None = None
        self._stopping = asyncio.Event()
        self._run_lock = asyncio.Lock()
        self._consecutive_failures = 0
        self._next_delay_seconds = self.interval_seconds
        self._recover_interrupted_runs()

    def _recover_interrupted_runs(self) -> int:
        recovered = 0
        with self.store.connect() as connection:
            rows = connection.execute(
                "SELECT run_id, started_at FROM scheduler_runs WHERE status='running'"
            ).fetchall()
            for row in rows:
                detail = {
                    "error": "scheduler process stopped before the run completed",
                    "recovered_at": utc_text(),
                    "previous_started_at": str(row["started_at"]),
                    "retry_policy": "retry_on_next_scheduler_cycle",
                }
                connection.execute(
                    """
                    UPDATE scheduler_runs
                    SET finished_at=?, status='interrupted', detail_encrypted=?
                    WHERE run_id=?
                    """,
                    (utc_text(), protect_text(_json(detail)), row["run_id"]),
                )
                recovered += 1
        return recovered

    @property
    def interval_seconds(self) -> int:
        return max(60, min(86400, int(self.store.get_setting("scheduler_interval_seconds", 900) or 900)))

    def configure(self, interval_seconds: int) -> dict[str, Any]:
        interval = max(60, min(86400, int(interval_seconds)))
        self.store.set_setting("scheduler_interval_seconds", interval)
        self.store.audit("scheduler_configured", {"interval_seconds": interval})
        return self.status()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._recover_interrupted_runs()
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="investment-background-scheduler")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _loop(self) -> None:
        delay = self.interval_seconds
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
            except asyncio.TimeoutError:
                try:
                    result = await self.run_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    result = {"status": "failed", "detail": {"error": str(exc)}}
                if result.get("status") == "failed":
                    self._consecutive_failures += 1
                    delay = min(
                        self.interval_seconds,
                        max(60, 60 * (2 ** min(6, self._consecutive_failures - 1))),
                    )
                else:
                    self._consecutive_failures = 0
                    delay = self.interval_seconds
                self._next_delay_seconds = delay

    async def run_once(self) -> dict[str, Any]:
        async with self._run_lock:
            run_id = uuid.uuid4().hex
            started = utc_text()
            with self.store.connect() as connection:
                connection.execute("INSERT INTO scheduler_runs VALUES(?, 'investment_intelligence', ?, '', 'running', '')", (run_id, started))
            try:
                detail = await self.cycle_callback()
                status = "completed"
            except Exception as exc:
                detail = {"error": str(exc)}
                status = "failed"
                try:
                    self.notifications.queue("投資管家排程失敗", str(exc), severity="warning")
                except Exception as notification_error:
                    detail["notification_queue_error"] = str(notification_error)
            try:
                notification_result = self.notifications.dispatch()
                if isinstance(detail, dict):
                    detail["notification_dispatch"] = notification_result
            except Exception as exc:
                if isinstance(detail, dict):
                    detail["notification_dispatch_error"] = str(exc)
                if status == "completed":
                    status = "completed_with_warnings"
            try:
                self._daily_backup()
            except Exception as exc:
                if isinstance(detail, dict):
                    detail["backup_error"] = str(exc)
                if status == "completed":
                    status = "completed_with_warnings"
            with self.store.connect() as connection:
                connection.execute(
                    "UPDATE scheduler_runs SET finished_at=?, status=?, detail_encrypted=? WHERE run_id=?",
                    (utc_text(), status, protect_text(_json(detail)), run_id),
                )
            self._prune_scheduler_runs()
            return {"run_id": run_id, "started_at": started, "status": status, "detail": detail}

    def _daily_backup(self) -> None:
        latest = self.store.get_setting("last_automatic_backup", "")
        parsed = parse_datetime(latest)
        if parsed and utc_now() - parsed < timedelta(hours=20):
            return
        if self.backup_callback is None:
            self.store.backup_database("automatic")
        else:
            self.backup_callback("automatic")
        self.store.set_setting("last_automatic_backup", utc_text())

    def _prune_scheduler_runs(self) -> int:
        """Retain every run; older rows form a logical append-only archive."""

        keep = max(
            20,
            min(
                2000,
                int(self.store.get_setting("scheduler_run_retention_count", 200) or 200),
            ),
        )
        with self.store.connect() as connection:
            total_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM scheduler_runs"
                ).fetchone()[0]
            )
        logical_archive_count = max(0, total_count - keep)
        pressure_threshold = max(5000, keep * 5)
        self.store.set_setting(
            "scheduler_run_storage",
            {
                "policy": "append_only_logical_archive",
                "active_window_count": min(total_count, keep),
                "archive_count": logical_archive_count,
                "total_count": total_count,
                "storage_pressure": total_count >= pressure_threshold,
                "pressure_threshold": pressure_threshold,
                "checked_at": utc_text(),
                "automatic_delete": False,
            },
        )
        return 0

    def status(self) -> dict[str, Any]:
        with self.store.connect() as connection:
            rows = connection.execute("SELECT run_id, job_name, started_at, finished_at, status, detail_encrypted FROM scheduler_runs ORDER BY started_at DESC LIMIT 20").fetchall()
        runs = []
        for source in rows:
            item = dict(source)
            item["detail"] = json.loads(unprotect_text(item.pop("detail_encrypted")) or "{}")
            runs.append(item)
        return {
            "running": bool(self._task and not self._task.done()),
            "interval_seconds": self.interval_seconds,
            "consecutive_failures": self._consecutive_failures,
            "next_delay_seconds": self._next_delay_seconds,
            "retry_policy": "bounded exponential retry after failed scheduled cycles",
            "storage": self.store.get_setting(
                "scheduler_run_storage",
                {
                    "policy": "append_only_logical_archive",
                    "automatic_delete": False,
                },
            ),
            "last_run": runs[0] if runs else None,
            "runs": runs,
        }
