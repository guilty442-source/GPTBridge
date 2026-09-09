from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..integration.adapters import PLATFORMS
from ..integration.browser_session import BrowserSessionManager
from ..infrastructure.repository import VaultlyRepository
from .vaultly_accounts import VaultlyAccountsMixin
from .vaultly_destination import VaultlyDestinationMixin
from .vaultly_diagnostics import VaultlyDiagnosticsMixin
from .vaultly_jobs import VaultlyJobsMixin
from .vaultly_link_jobs import VaultlyLinkJobsMixin
from .vaultly_media import VaultlyMediaMixin
from .vaultly_post_scan import VaultlyPostScanMixin
from .vaultly_scan import VaultlyScanMixin
from .vaultly_state import VaultlyStateMixin
from .vaultly_utils import VaultlyUtilsMixin


class VaultlyService(
    VaultlyUtilsMixin,
    VaultlyDiagnosticsMixin,
    VaultlyStateMixin,
    VaultlyDestinationMixin,
    VaultlyScanMixin,
    VaultlyAccountsMixin,
    VaultlyPostScanMixin,
    VaultlyJobsMixin,
    VaultlyLinkJobsMixin,
    VaultlyMediaMixin,
):
    """Vaultly download and monitoring service.

    Composed from focused mixin modules — each mixin owns a logical
    group of methods (state, scanning, jobs, media, etc.).  All public
    behaviour is identical to the original monolithic implementation.
    """

    VERSION = "1.0.0"
    AUTO_SCAN_SUCCESS_INTERVAL_SECONDS = 30 * 60
    AUTO_SCAN_RETRY_INTERVAL_SECONDS = 30
    MAX_MEDIA_BYTES = 150 * 1024 * 1024
    DOWNLOAD_RETRY_ATTEMPTS = 3
    DOWNLOAD_RETRY_BACKOFF_SECONDS = 0.35
    COMMANDS = {
        "vaultly_get_state",
        "vaultly_check_destination",
        "vaultly_open_platform",
        "vaultly_scan_following",
        "vaultly_add_filter_terms",
        "vaultly_remove_filter_terms",
        "vaultly_remove_accounts",
        "vaultly_restore_accounts",
        "vaultly_save_selection",
        "vaultly_scan_posts",
        "vaultly_cancel_post_scan",
        "vaultly_create_job",
        "vaultly_create_link_job",
        "vaultly_retry_job",
        "vaultly_cancel_job",
        "vaultly_export_report",
    }

    def __init__(self, project_root: Path, session: Any | None = None) -> None:
        self.project_root = project_root
        self._owns_session = session is None
        self.session = session or BrowserSessionManager(
            profile_name="vaultly",
            headless=False,
            profile_root=project_root / "runtime" / "browser-profiles",
        )
        self.repository = VaultlyRepository(project_root)
        self._job_lock = asyncio.Lock()
        self._job_tasks: dict[str, asyncio.Task[Any]] = {}
        self._post_scan_tasks: dict[str, asyncio.Task[Any]] = {}
        self._pending_start_job_ids: set[str] = set()
        self._pending_start_post_scan_ids: set[str] = set()
        self._cancelled_jobs: set[str] = set()
        self._cancelled_post_scan_jobs: set[str] = set()
        self._scan_locks = {platform: asyncio.Lock() for platform in PLATFORMS}
        self._post_scan_locks = {platform: asyncio.Lock() for platform in PLATFORMS}
        self._scan_continuations = {platform: False for platform in PLATFORMS}
        self._user_opened = False
        self._browser_session_requested = False
        self._auto_scan_task: asyncio.Task[Any] | None = None
        self._auto_scan_next_due = {platform: 0.0 for platform in PLATFORMS}
        self._auto_scan_state: dict[str, dict[str, Any]] = {
            platform: {
                "status": "waiting_login",
                "last_scan_at": "",
                "message": "等待開啟影音下載自動化",
            }
            for platform in PLATFORMS
        }

    @property
    def workspace(self) -> Any:
        class Workspace:
            workspace_root = self.project_root

        return Workspace()

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def start(self) -> None:
        for job_id in self.repository.requeue_interrupted_jobs():
            if self._user_opened and self._can_start_browser_work():
                self._schedule_job(job_id)
            else:
                self._pending_start_job_ids.add(job_id)
        for scan_job_id in self.repository.requeue_interrupted_post_scan_jobs():
            if self._user_opened and self._can_start_browser_work():
                self._schedule_post_scan_job(scan_job_id)
            else:
                self._pending_start_post_scan_ids.add(scan_job_id)
        if self._auto_scan_task is None or self._auto_scan_task.done():
            self._auto_scan_task = asyncio.create_task(self._auto_scan_loop())

    async def shutdown(self) -> None:
        if self._auto_scan_task is not None and not self._auto_scan_task.done():
            self._auto_scan_task.cancel()
            await asyncio.gather(self._auto_scan_task, return_exceptions=True)
        self._auto_scan_task = None
        tasks = [task for task in self._job_tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._job_tasks.clear()
        post_scan_tasks = [
            task for task in self._post_scan_tasks.values() if not task.done()
        ]
        for task in post_scan_tasks:
            task.cancel()
        if post_scan_tasks:
            await asyncio.gather(*post_scan_tasks, return_exceptions=True)
        self._post_scan_tasks.clear()
        if self._owns_session:
            await self.session.shutdown()

    async def handle(
        self,
        command: str,
        payload: dict[str, Any],
        latest_ai_answer: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        del latest_ai_answer
        handlers = {
            "vaultly_get_state": self._get_state,
            "vaultly_check_destination": self._check_destination,
            "vaultly_open_platform": self._open_platform,
            "vaultly_scan_following": self._scan_following,
            "vaultly_add_filter_terms": self._add_filter_terms,
            "vaultly_remove_filter_terms": self._remove_filter_terms,
            "vaultly_remove_accounts": self._remove_accounts,
            "vaultly_restore_accounts": self._restore_accounts,
            "vaultly_save_selection": self._save_selection,
            "vaultly_scan_posts": self._scan_posts,
            "vaultly_cancel_post_scan": self._cancel_post_scan,
            "vaultly_create_job": self._create_job,
            "vaultly_create_link_job": self._create_link_job,
            "vaultly_retry_job": self._retry_job,
            "vaultly_cancel_job": self._cancel_job,
            "vaultly_export_report": self._export_report,
        }
        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {"ok": False, "message": "不支援的 Vaultly 指令"}
        try:
            return f"{command}_result", await handler(payload)
        except Exception as exc:
            return f"{command}_result", {"ok": False, "message": str(exc)}
