from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urlparse, urlunparse

try:
    import imageio_ffmpeg
except ModuleNotFoundError:
    class _MissingImageioFfmpeg:
        @staticmethod
        def get_ffmpeg_exe() -> str:
            raise RuntimeError("缺少 imageio_ffmpeg，無法合併 HLS 影片。")

    imageio_ffmpeg = _MissingImageioFfmpeg()

from .adapters import PLATFORMS, get_adapter
from .browser_session import BrowserSessionManager
from .repository import VaultlyRepository
from .rules import (
    build_download_filename,
    extension_for_media,
    is_allowed_media_url,
    is_valid_media_file,
    is_valid_media_payload,
    media_matches_conditions,
    normalize_conditions,
    post_matches_conditions,
)


class VaultlyService:
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
            workspace_root = self.project_root / "platform_tools" / "vaultly"

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

    async def _get_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._mark_user_opened()
        accounts = self.repository.list_accounts()
        jobs = self._jobs_with_automation(self.repository.list_jobs())
        post_scan_jobs = self._post_scan_jobs_with_automation(
            self.repository.list_post_scan_jobs()
        )
        post_query = payload.get("posts", {})
        if not isinstance(post_query, dict):
            post_query = {}
        try:
            posts_limit = int(post_query.get("limit", 40) or 40)
        except (TypeError, ValueError):
            posts_limit = 40
        try:
            posts_offset = int(post_query.get("offset", 0) or 0)
        except (TypeError, ValueError):
            posts_offset = 0
        posts_limit = max(1, min(100, posts_limit))
        posts_offset = max(0, posts_offset)
        posts_platform = str(post_query.get("platform", "")).strip()
        posts_status = str(post_query.get("status", "")).strip()
        posts_search = str(post_query.get("query", "")).strip()
        posts_total = self.repository.count_posts(
            platform=posts_platform,
            status=posts_status,
            query=posts_search,
        )
        destination = self.repository.get_setting("destination", "")
        removed_accounts = self.repository.list_removed_accounts()
        destination_health = self._destination_health(destination)
        diagnostics = self._diagnostics(
            accounts,
            removed_accounts,
            jobs,
            post_scan_jobs,
            destination_health,
        )
        return {
            "ok": True,
            "version": self.VERSION,
            "platforms": [
                {
                    "id": definition.id,
                    "name": definition.name,
                    "home_url": definition.home_url,
                }
                for definition in PLATFORMS.values()
            ],
            "accounts": accounts,
            "filter_terms": self.repository.list_filter_terms(),
            "removed_accounts": removed_accounts,
            "auto_scan": self._auto_scan_state,
            "jobs": jobs,
            "post_scan_jobs": post_scan_jobs,
            "download_automation": self._download_automation_summary(
                jobs,
                post_scan_jobs,
            ),
            "scan_schedule": self.repository.list_account_scan_schedule(),
            "posts": self.repository.list_posts(
                limit=posts_limit,
                offset=posts_offset,
                platform=posts_platform,
                status=posts_status,
                query=posts_search,
            ),
            "posts_total": posts_total,
            "posts_offset": posts_offset,
            "posts_limit": posts_limit,
            "destination": destination,
            "destination_health": destination_health,
            "diagnostics": diagnostics,
            "database_path": str(self.repository.db_path),
            "workspace_path": str(self.project_root / "platform_tools" / "vaultly"),
            "browser_profile_path": str(getattr(self.session, "shared_profile_dir", "")),
            "active_job_id": next(
                (job["job_id"] for job in jobs if job["status"] == "running"),
                "",
            ),
            "safety_notice": "只處理目前登入帳號可見的內容，不繞過私密權限、登入、平台限制或 DRM。",
        }

    def _diagnostics(
        self,
        accounts: list[dict[str, Any]],
        removed_accounts: list[dict[str, Any]],
        jobs: list[dict[str, Any]],
        post_scan_jobs: list[dict[str, Any]],
        destination_health: dict[str, Any],
    ) -> dict[str, Any]:
        platform_health = self._platform_health(accounts, removed_accounts)
        failure_summary = self._failure_summary(jobs, post_scan_jobs)
        browser = self._browser_health()
        active_work = any(job.get("status") in {"queued", "running"} for job in jobs) or any(
            job.get("status") in {"queued", "running"} for job in post_scan_jobs
        )

        if failure_summary["total_failures"] > 0:
            state = "attention"
            message = "已有失敗項目，建議匯出診斷報告檢查。"
        elif active_work:
            state = "running"
            message = "背景工作執行中。"
        elif not destination_health.get("ok"):
            state = "setup"
            message = "請先確認下載資料夾。"
        elif not browser["initialized"]:
            state = "waiting_login"
            message = "等待開啟登入頁並完成登入。"
        else:
            state = "ready"
            message = "系統可用。"

        return {
            "state": state,
            "message": message,
            "browser": browser,
            "destination": destination_health,
            "platforms": platform_health,
            "failure_summary": failure_summary,
            "generated_at": self._now(),
        }

    def _browser_health(self) -> dict[str, Any]:
        context = getattr(self.session, "context", None)
        pages = getattr(context, "pages", []) if context is not None else []
        try:
            page_count = len(pages)
        except TypeError:
            page_count = 0
        return {
            "initialized": bool(getattr(self.session, "is_initialized", False)),
            "requested": self._browser_session_requested,
            "user_opened": self._user_opened,
            "page_count": page_count,
            "profile_path": str(getattr(self.session, "shared_profile_dir", "")),
        }

    def _platform_health(
        self,
        accounts: list[dict[str, Any]],
        removed_accounts: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for platform, definition in PLATFORMS.items():
            auto_state = self._auto_scan_state.get(platform, {})
            status = str(auto_state.get("status", "waiting_login") or "waiting_login")
            platform_accounts = [
                account for account in accounts if account.get("platform") == platform
            ]
            removed_count = sum(
                1 for account in removed_accounts if account.get("platform") == platform
            )
            if status == "error":
                health = "attention"
            elif status == "scanning":
                health = "running"
            elif platform_accounts:
                health = "ready"
            else:
                health = "login_required"
            output.append(
                {
                    "id": platform,
                    "name": definition.name,
                    "health": health,
                    "status": status,
                    "message": str(auto_state.get("message", "")),
                    "last_scan_at": str(auto_state.get("last_scan_at", "")),
                    "account_count": len(platform_accounts),
                    "selected_count": sum(1 for account in platform_accounts if account.get("selected")),
                    "removed_count": removed_count,
                }
            )
        return output

    @classmethod
    def _failure_summary(
        cls,
        jobs: list[dict[str, Any]],
        post_scan_jobs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        failures: list[dict[str, Any]] = []
        for job in jobs:
            failed_count = cls._as_int(job.get("failed"))
            if job.get("status") == "failed" or failed_count > 0:
                message = str(job.get("message", "") or "")
                failures.append(
                    {
                        "kind": "download",
                        "id": str(job.get("job_id", "")),
                        "status": str(job.get("status", "")),
                        "failed": failed_count,
                        "message": message,
                        "category": cls._failure_category(message),
                    }
                )
        for job in post_scan_jobs:
            failed_count = cls._as_int(job.get("failed"))
            if job.get("status") == "failed" or failed_count > 0:
                message = str(job.get("message", "") or "")
                failures.append(
                    {
                        "kind": "post_scan",
                        "id": str(job.get("scan_job_id", "")),
                        "status": str(job.get("status", "")),
                        "failed": failed_count,
                        "message": message,
                        "category": cls._failure_category(message),
                    }
                )

        category_counts: dict[str, int] = {}
        for failure in failures:
            category = str(failure["category"])
            category_counts[category] = category_counts.get(category, 0) + 1

        latest = failures[0] if failures else {}
        return {
            "total_failures": len(failures),
            "total_failed_items": sum(cls._as_int(item.get("failed")) for item in failures),
            "categories": category_counts,
            "latest": latest,
            "items": failures[:10],
        }

    @staticmethod
    def _failure_category(message: str) -> str:
        folded = message.casefold()
        if any(marker in folded for marker in ("資料夾", "folder", "permission", "權限", "write", "磁碟", "space")):
            return "destination"
        if any(marker in folded for marker in ("login", "登入", "cookie", "browser_locked", "profile")):
            return "login"
        if any(marker in folded for marker in ("http 429", "http 5", "timeout", "timed out", "network", "connection")):
            return "network"
        if any(marker in folded for marker in ("ffmpeg", "hls", "容器", "影片", "media", "媒體")):
            return "media"
        if any(marker in folded for marker in ("instagram", "x ", "following", "追蹤", "個人檔案")):
            return "platform"
        if any(marker in folded for marker in ("cancel", "取消")):
            return "cancelled"
        return "unknown"

    async def _export_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        state = await self._get_state({})
        report = {
            "version": self.VERSION,
            "generated_at": self._now(),
            "diagnostics": state.get("diagnostics", {}),
            "download_automation": state.get("download_automation", {}),
            "platforms": state.get("platforms", []),
            "jobs": state.get("jobs", [])[:20],
            "post_scan_jobs": state.get("post_scan_jobs", [])[:20],
            "paths": {
                "workspace": state.get("workspace_path", ""),
                "database": state.get("database_path", ""),
                "browser_profile": state.get("browser_profile_path", ""),
            },
        }
        export_dir = self.project_root / "runtime" / "exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        report_path = export_dir / f"vaultly-diagnostic-{timestamp}.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return {
            "ok": True,
            "report_path": str(report_path),
            "message": "診斷報告已匯出",
        }

    @classmethod
    def _jobs_with_automation(cls, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **job,
                "automation_summary": cls._job_automation_summary(job),
            }
            for job in jobs
        ]

    @classmethod
    def _post_scan_jobs_with_automation(
        cls,
        jobs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return [
            {
                **job,
                "automation_summary": cls._post_scan_automation_summary(job),
            }
            for job in jobs
        ]

    @classmethod
    def _download_automation_summary(
        cls,
        jobs: list[dict[str, Any]],
        post_scan_jobs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        running_jobs = sum(1 for job in jobs if job.get("status") == "running")
        queued_jobs = sum(1 for job in jobs if job.get("status") == "queued")
        running_scans = sum(1 for job in post_scan_jobs if job.get("status") == "running")
        queued_scans = sum(1 for job in post_scan_jobs if job.get("status") == "queued")
        downloaded = sum(cls._as_int(job.get("downloaded")) for job in jobs)
        failed = sum(cls._as_int(job.get("failed")) for job in jobs)
        attempts = downloaded + failed
        active_job = next(
            (job for job in jobs if job.get("status") in {"running", "queued"}),
            None,
        )
        active_scan = next(
            (job for job in post_scan_jobs if job.get("status") in {"running", "queued"}),
            None,
        )
        return {
            "running_jobs": running_jobs,
            "queued_jobs": queued_jobs,
            "running_post_scans": running_scans,
            "queued_post_scans": queued_scans,
            "queue_depth": queued_jobs + queued_scans,
            "has_active_work": bool(running_jobs or queued_jobs or running_scans or queued_scans),
            "active_job_id": str(active_job.get("job_id", "")) if active_job else "",
            "active_scan_id": str(active_scan.get("scan_job_id", "")) if active_scan else "",
            "total_downloaded": downloaded,
            "total_failed": failed,
            "success_rate": round(downloaded / attempts * 100, 1) if attempts else 0,
            "status_counts": cls._status_counts(jobs),
            "post_scan_status_counts": cls._status_counts(post_scan_jobs),
            "retry_attempts": cls.DOWNLOAD_RETRY_ATTEMPTS,
            "max_media_bytes": cls.MAX_MEDIA_BYTES,
        }

    @classmethod
    def _job_automation_summary(cls, job: dict[str, Any]) -> dict[str, Any]:
        total = cls._as_int(job.get("progress_total"))
        current = cls._bounded_current(job.get("progress_current"), total)
        matched = cls._as_int(job.get("matched"))
        downloaded = cls._as_int(job.get("downloaded"))
        skipped = cls._as_int(job.get("skipped"))
        failed = cls._as_int(job.get("failed"))
        processed = matched + skipped + failed if job.get("preview_only") else downloaded + skipped + failed
        elapsed_seconds = cls._elapsed_seconds(job)
        attempts = downloaded + failed
        progress_percent = cls._progress_percent(current, total)
        if job.get("status") == "completed":
            progress_percent = 100
        return {
            "progress_percent": progress_percent,
            "processed": processed,
            "remaining": max(total - current, 0) if total else 0,
            "elapsed_seconds": elapsed_seconds,
            "throughput_per_minute": (
                round(downloaded / elapsed_seconds * 60, 2)
                if elapsed_seconds > 0 and downloaded > 0
                else 0
            ),
            "success_rate": round(downloaded / attempts * 100, 1) if attempts else 0,
            "failure_rate": round(failed / attempts * 100, 1) if attempts else 0,
            "is_active": job.get("status") in {"queued", "running"},
            "label": cls._automation_label(str(job.get("status", "")), failed),
        }

    @classmethod
    def _post_scan_automation_summary(cls, job: dict[str, Any]) -> dict[str, Any]:
        total = cls._as_int(job.get("progress_total"))
        current = cls._bounded_current(job.get("progress_current"), total)
        discovered = cls._as_int(job.get("discovered"))
        inspected = cls._as_int(job.get("inspected"))
        skipped_existing = cls._as_int(job.get("skipped_existing"))
        failed = cls._as_int(job.get("failed"))
        elapsed_seconds = cls._elapsed_seconds(job)
        progress_percent = cls._progress_percent(current, total)
        if job.get("status") == "completed":
            progress_percent = 100
        return {
            "progress_percent": progress_percent,
            "processed": inspected + skipped_existing + failed,
            "remaining": max(total - current, 0) if total else 0,
            "elapsed_seconds": elapsed_seconds,
            "throughput_per_minute": (
                round(inspected / elapsed_seconds * 60, 2)
                if elapsed_seconds > 0 and inspected > 0
                else 0
            ),
            "discovered": discovered,
            "success_rate": (
                round(inspected / (inspected + failed) * 100, 1)
                if inspected + failed
                else 0
            ),
            "failure_rate": (
                round(failed / (inspected + failed) * 100, 1)
                if inspected + failed
                else 0
            ),
            "is_active": job.get("status") in {"queued", "running"},
            "label": cls._automation_label(str(job.get("status", "")), failed),
        }

    @staticmethod
    def _automation_label(status: str, failed: int) -> str:
        if status == "queued":
            return "等待中"
        if status == "running":
            return "自動化執行中" if failed == 0 else "執行中，已有失敗項目"
        if status == "completed":
            return "已完成" if failed == 0 else "已完成，需檢查失敗項目"
        if status == "cancelled":
            return "已取消"
        if status == "failed":
            return "工作失敗"
        return "待命"

    @classmethod
    def _elapsed_seconds(cls, item: dict[str, Any]) -> int:
        started_at = cls._parse_iso_timestamp(item.get("started_at"))
        if started_at is None:
            return 0
        finished_at = cls._parse_iso_timestamp(item.get("finished_at"))
        if finished_at is None and item.get("status") in {"queued", "running"}:
            finished_at = datetime.now(timezone.utc)
        if finished_at is None:
            return 0
        return max(0, int((finished_at - started_at).total_seconds()))

    @staticmethod
    def _parse_iso_timestamp(value: Any) -> datetime | None:
        text = str(value or "").strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _progress_percent(current: int, total: int) -> int:
        if total <= 0:
            return 0
        return max(0, min(100, round(current / total * 100)))

    @classmethod
    def _bounded_current(cls, value: Any, total: int) -> int:
        current = max(0, cls._as_int(value))
        return min(current, total) if total > 0 else current

    @staticmethod
    def _status_counts(items: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in items:
            status = str(item.get("status", "unknown") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        return counts

    @staticmethod
    def _as_int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    async def _check_destination(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = str(payload.get("path", "")).strip()
        return {
            "ok": True,
            "destination_health": self._destination_health(path),
        }

    def _validate_destination(
        self,
        destination_text: str,
        required: bool,
    ) -> tuple[Path | None, dict[str, Any]]:
        health = self._destination_health(destination_text)
        if required and not health["ok"]:
            raise ValueError(str(health["message"]))
        if not health["ok"]:
            return None, health
        path = Path(str(health.get("path", ""))).expanduser() if health.get("path") else None
        return path, health

    def _destination_health(self, destination_text: str) -> dict[str, Any]:
        raw_path = str(destination_text or "").strip()
        if not raw_path:
            return {
                "ok": False,
                "path": "",
                "exists": False,
                "is_dir": False,
                "writable": False,
                "free_bytes": 0,
                "message": "尚未選擇下載資料夾",
            }
        try:
            path = Path(raw_path).expanduser()
            resolved = path.resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            return {
                "ok": False,
                "path": raw_path,
                "exists": False,
                "is_dir": False,
                "writable": False,
                "free_bytes": 0,
                "message": f"下載資料夾路徑無效：{self._short_error(exc)}",
            }
        exists = resolved.exists()
        is_dir = resolved.is_dir() if exists else False
        free_bytes = 0
        writable = False
        message = "下載資料夾可用"
        if exists and is_dir:
            with contextlib.suppress(OSError):
                free_bytes = int(shutil.disk_usage(resolved).free)
            writable = self._can_write_to_directory(resolved)
            if not writable:
                message = "下載資料夾無法寫入，請更換位置或調整權限"
            elif free_bytes and free_bytes < 100 * 1024 * 1024:
                message = "下載資料夾可寫入，但剩餘空間低於 100 MB"
        elif not exists:
            message = "下載資料夾不存在"
        else:
            message = "下載位置不是資料夾"
        return {
            "ok": bool(exists and is_dir and writable),
            "path": str(resolved),
            "exists": exists,
            "is_dir": is_dir,
            "writable": writable,
            "free_bytes": free_bytes,
            "message": message,
        }

    @staticmethod
    def _can_write_to_directory(path: Path) -> bool:
        probe = path / f".vaultly-write-test-{uuid.uuid4().hex}.tmp"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return True
        except OSError:
            with contextlib.suppress(OSError):
                probe.unlink()
            return False

    async def _open_platform(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._mark_user_opened()
        platform = str(payload.get("platform", "")).strip()
        definition = PLATFORMS.get(platform)
        if definition is None:
            return {"ok": False, "message": "請選擇支援的平台"}
        self._mark_browser_session_requested()
        page = await self.session.ensure_external_page(
            f"vaultly:user:{platform}",
            definition.home_url,
            (),
        )
        await page.bring_to_front()
        return {
            "ok": True,
            "platform": platform,
            "url": page.url,
            "message": f"已開啟 {definition.name}。完成登入後，Vaultly 會自動掃描追蹤名單。",
        }

    async def _scan_following(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._mark_user_opened()
        platform = str(payload.get("platform", "")).strip()
        try:
            return await self._scan_platform(platform, automatic=False)
        except Exception as exc:
            if platform in PLATFORMS:
                self._scan_continuations[platform] = False
                self._set_auto_scan_state(
                    platform,
                    "error",
                    f"掃描暫時失敗：{exc}",
                )
            raise

    async def _scan_platform(
        self,
        platform: str,
        automatic: bool,
    ) -> dict[str, Any]:
        definition = PLATFORMS.get(platform)
        if definition is None:
            return {"ok": False, "message": "請選擇支援的平台"}
        if not automatic:
            self._mark_browser_session_requested()
        async with self._scan_locks[platform]:
            self._set_auto_scan_state(
                platform,
                "scanning",
                "正在自動掃描追蹤名單" if automatic else "正在重新掃描追蹤名單",
            )
            page = await self.session.ensure_external_page(
                f"vaultly:scan:{platform}",
                definition.home_url,
                (),
            )
            adapter = get_adapter(platform)
            prepare_scan = getattr(adapter, "prepare_following_scan", None)
            ready = await prepare_scan(page) if callable(prepare_scan) else True
            if not ready:
                prepare_status = getattr(adapter, "last_prepare_status", {})
                message = (
                    str(prepare_status.get("message", "")).strip()
                    if isinstance(prepare_status, dict)
                    else ""
                ) or f"{definition.name} 尚未登入；登入後會自動開始掃描。"
                status = (
                    str(prepare_status.get("status", "waiting_login")).strip()
                    if isinstance(prepare_status, dict)
                    else "waiting_login"
                )
                self._scan_continuations[platform] = False
                self._set_auto_scan_state(platform, status or "waiting_login", message)
                return {"ok": False, "platform": platform, "message": message}

            existing_accounts = [
                account
                for account in self.repository.list_accounts()
                if account.get("platform") == platform
            ]
            retained_account_ids = set(self.repository.list_retained_account_ids())
            retained_account_ids.update(
                str(account.get("account_id", ""))
                for account in existing_accounts
                if account.get("verified") is True
            )
            manually_removed_account_ids = {
                str(account.get("account_id", ""))
                for account in self.repository.list_removed_accounts()
                if account.get("platform") == platform
                and account.get("source") == "manual"
            }
            accounts = await adapter.scan_following(
                page,
                filter_terms=self.repository.list_filter_terms(),
                retained_account_ids=sorted(retained_account_ids),
                reset_to_start=not (
                    automatic and self._scan_continuations[platform]
                ),
            )
            hidden_manual_removed_count = 0
            if manually_removed_account_ids:
                visible_accounts = []
                for account in accounts:
                    account_id = str(account.get("account_id", ""))
                    if account_id in manually_removed_account_ids:
                        hidden_manual_removed_count += 1
                        continue
                    visible_accounts.append(account)
                accounts = visible_accounts
            reused_avatar_count = self._reuse_cached_instagram_avatars(
                accounts,
                existing_accounts,
            )
            await adapter.hydrate_avatar_urls(page, accounts)
            scan_stats = getattr(adapter, "last_scan_stats", {})
            observed_count = int(scan_stats.get("observed", 0) or 0)
            scan_completed = bool(scan_stats.get("completed", True))
            self._scan_continuations[platform] = not scan_completed
            if platform == "instagram" and observed_count == 0:
                message = "Instagram 追蹤名單已開啟，但尚未讀到帳號；稍後會自動重試。"
                self._scan_continuations[platform] = False
                self._set_auto_scan_state(platform, "error", message)
                return {"ok": False, "platform": platform, "message": message}
            filtered_accounts = scan_stats.get("filtered_accounts", [])
            if not isinstance(filtered_accounts, list):
                filtered_accounts = []
            filtered_ids = [
                str(account.get("account_id", ""))
                for account in filtered_accounts
                if isinstance(account, dict) and str(account.get("account_id", ""))
            ]
            self.repository.record_removed_accounts(filtered_accounts)
            self.repository.delete_accounts(filtered_ids)
            self.repository.upsert_accounts(accounts)
            self.repository.clear_removed_accounts(
                str(account.get("account_id", "")) for account in accounts
            )
            filtered_count = (
                int(scan_stats.get("filtered", len(filtered_ids)) or 0)
                + hidden_manual_removed_count
            )
            rounds = int(scan_stats.get("rounds", 0) or 0)
            scan_summary = (
                f"掃描 {observed_count} 個帳號"
                + (f"／{rounds} 輪" if rounds else "")
                + f"，保留 {len(accounts)} 個候選帳號，移除 {filtered_count} 個帳號。"
            )
            if scan_completed:
                message = (
                    f"自動掃描完成：{scan_summary}"
                    if automatic
                    else f"掃描完成：{scan_summary}"
                )
            else:
                message = f"掃描暫存：{scan_summary}清單尚未掃描到底，稍後會自動重試。"
            self._set_auto_scan_state(
                platform,
                "completed" if scan_completed else "error",
                message,
                last_scan_at=self._now(),
            )
            return {
                "ok": True,
                "platform": platform,
                "count": len(accounts),
                "filtered_count": filtered_count,
                "observed_count": observed_count,
                "reused_avatar_count": reused_avatar_count,
                "scan_complete": scan_completed,
                "scan_stats": scan_stats,
                "accounts": self.repository.list_accounts(),
                "message": message,
            }

    async def _auto_scan_loop(self) -> None:
        await asyncio.sleep(2)
        while True:
            try:
                if not self._user_opened:
                    await asyncio.sleep(10)
                    continue
                if not await self._ensure_auto_scan_session():
                    await asyncio.sleep(self.AUTO_SCAN_RETRY_INTERVAL_SECONDS)
                    continue

                current = monotonic()
                due_platforms = [
                    platform
                    for platform in PLATFORMS
                    if current >= self._auto_scan_next_due[platform]
                ]
                if due_platforms:
                    await asyncio.gather(
                        *(self._run_auto_scan(platform) for platform in due_platforms)
                    )
                    await asyncio.sleep(2)
                else:
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise

    async def _ensure_auto_scan_session(self) -> bool:
        if getattr(self.session, "is_initialized", False):
            return True
        if not self._browser_session_requested:
            for platform, current in self._auto_scan_state.items():
                if current.get("status") == "waiting_login":
                    self._set_auto_scan_state(
                        platform,
                        "waiting_login",
                        "等待開啟登入頁後自動掃描",
                    )
            return False
        ensure_initialized = getattr(self.session, "ensure_initialized", None)
        if not callable(ensure_initialized):
            return False
        try:
            await ensure_initialized()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for platform in PLATFORMS:
                self._set_auto_scan_state(
                    platform,
                    "error",
                    f"自動掃描瀏覽器暫時無法啟動：{exc}",
                )
            return False
        return bool(getattr(self.session, "is_initialized", False))

    async def _run_auto_scan(self, platform: str) -> None:
        try:
            result = await self._scan_platform(platform, automatic=True)
            interval = (
                self.AUTO_SCAN_SUCCESS_INTERVAL_SECONDS
                if result.get("ok") is True and result.get("scan_complete", True) is True
                else self.AUTO_SCAN_RETRY_INTERVAL_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._scan_continuations[platform] = False
            interval = self.AUTO_SCAN_RETRY_INTERVAL_SECONDS
            self._set_auto_scan_state(
                platform,
                "error",
                f"自動掃描暫時失敗：{exc}",
            )
        self._auto_scan_next_due[platform] = monotonic() + interval

    def _set_auto_scan_state(
        self,
        platform: str,
        status: str,
        message: str,
        last_scan_at: str | None = None,
    ) -> None:
        current = self._auto_scan_state.get(platform, {})
        self._auto_scan_state[platform] = {
            "status": status,
            "message": message,
            "last_scan_at": (
                last_scan_at
                if last_scan_at is not None
                else str(current.get("last_scan_at", ""))
            ),
        }

    def _mark_user_opened(self) -> None:
        first_open = not self._user_opened
        self._user_opened = True
        if first_open:
            for platform, current in self._auto_scan_state.items():
                if current.get("status") == "waiting_login":
                    self._set_auto_scan_state(
                        platform,
                        "waiting_login",
                        "等待登入後自動掃描",
                    )
                self._auto_scan_next_due[platform] = 0.0
        self._flush_pending_browser_work()

    def _mark_browser_session_requested(self) -> None:
        self._browser_session_requested = True
        self._mark_user_opened()

    def _can_start_browser_work(self) -> bool:
        return self._browser_session_requested or bool(
            getattr(self.session, "is_initialized", False)
        )

    def _flush_pending_browser_work(self) -> None:
        if not self._user_opened or not self._can_start_browser_work():
            return
        for job_id in sorted(self._pending_start_job_ids):
            self._schedule_job(job_id)
        self._pending_start_job_ids.clear()
        for scan_job_id in sorted(self._pending_start_post_scan_ids):
            self._schedule_post_scan_job(scan_job_id)
        self._pending_start_post_scan_ids.clear()

    @staticmethod
    def _reuse_cached_instagram_avatars(
        accounts: list[dict[str, Any]],
        existing_accounts: list[dict[str, Any]],
    ) -> int:
        cached_avatars = {
            str(account.get("account_id", "")): str(account.get("avatar_url", ""))
            for account in existing_accounts
            if str(account.get("platform", "")) == "instagram"
            and str(account.get("avatar_url", "")).startswith("data:image/")
        }
        reused = 0
        for account in accounts:
            cached_avatar = cached_avatars.get(str(account.get("account_id", "")), "")
            if not cached_avatar:
                continue
            account["avatar_url"] = cached_avatar
            reused += 1
        return reused

    @staticmethod
    def _payload_terms(payload: dict[str, Any]) -> list[str]:
        raw_terms = payload.get("terms", [])
        if isinstance(raw_terms, str):
            raw_terms = raw_terms.replace("，", ",").replace("\n", ",").split(",")
        if not isinstance(raw_terms, list):
            return []
        return [str(term).strip() for term in raw_terms if str(term).strip()]

    @staticmethod
    def _payload_links(raw_links: Any) -> list[str]:
        if isinstance(raw_links, str):
            values = re.split(r"[\s,]+", raw_links)
        elif isinstance(raw_links, list):
            values = [str(item) for item in raw_links]
        else:
            values = []
        links: list[str] = []
        seen: set[str] = set()
        for value in values:
            match = re.search(r"https?://[^\s<>'\"]+", str(value).strip())
            if not match:
                continue
            link = match.group(0).rstrip(").,，、。")
            marker = link.casefold()
            if marker in seen:
                continue
            seen.add(marker)
            links.append(link)
        return links[:100]

    @staticmethod
    def _normalize_link_targets(links: list[str]) -> list[dict[str, str]]:
        targets: list[dict[str, str]] = []
        seen: set[str] = set()
        for link in links:
            parsed = urlparse(link)
            hostname = (parsed.hostname or "").casefold()
            path_parts = [part for part in parsed.path.split("/") if part]
            platform = ""
            if (
                hostname == "instagram.com"
                or hostname.endswith(".instagram.com")
            ) and path_parts:
                first = path_parts[0].casefold()
                if first in {"p", "reel", "reels", "tv", "stories", "share"}:
                    platform = "instagram"
            elif (
                hostname in {"x.com", "twitter.com"}
                or hostname.endswith(".x.com")
                or hostname.endswith(".twitter.com")
            ) and "status" in {part.casefold() for part in path_parts}:
                platform = "x"
            if not platform:
                continue
            normalized = urlunparse(
                (
                    parsed.scheme or "https",
                    parsed.netloc,
                    parsed.path.rstrip("/") or "/",
                    "",
                    "",
                    "",
                )
            )
            marker = normalized.casefold()
            if marker in seen:
                continue
            seen.add(marker)
            targets.append({"platform": platform, "url": normalized})
        return targets

    async def _add_filter_terms(self, payload: dict[str, Any]) -> dict[str, Any]:
        terms = self._payload_terms(payload)
        changed = self.repository.add_filter_terms(terms)
        exact_handles = {
            term.strip().lstrip("@").casefold()
            for term in terms
            if term.strip().startswith("@")
        }
        if exact_handles:
            self.repository.remove_retained_accounts(
                account_id
                for account_id in self.repository.list_retained_account_ids()
                if account_id.partition(":")[2].casefold() in exact_handles
            )
        for platform in self._auto_scan_next_due:
            self._auto_scan_next_due[platform] = 0
        return {
            "ok": True,
            "filter_terms": self.repository.list_filter_terms(),
            "message": f"已新增 {changed} 個篩選項目，下一輪自動掃描會套用。",
        }

    async def _remove_filter_terms(self, payload: dict[str, Any]) -> dict[str, Any]:
        changed = self.repository.remove_filter_terms(self._payload_terms(payload))
        return {
            "ok": True,
            "filter_terms": self.repository.list_filter_terms(),
            "message": f"已移除 {changed} 個篩選項目。",
        }

    async def _remove_accounts(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_ids = payload.get("account_ids", [])
        account_ids = raw_ids if isinstance(raw_ids, list) else []
        accounts = self.repository.get_accounts(account_ids)
        removable = accounts
        protected_count = 0
        for account in removable:
            account["filter_reason"] = "手動從保留名單移除"
            account["filter_source"] = "manual"
        self.repository.record_removed_accounts(removable)
        self.repository.delete_accounts(account["account_id"] for account in removable)
        self.repository.remove_retained_accounts(
            account["account_id"] for account in removable
        )
        return {
            "ok": True,
            "removed_count": len(removable),
            "protected_count": protected_count,
            "message": f"已移除 {len(removable)} 個帳號並記錄到移除紀錄。",
        }

    async def _restore_accounts(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_ids = payload.get("account_ids", [])
        account_ids = raw_ids if isinstance(raw_ids, list) else []
        restored = self.repository.restore_removed_accounts(account_ids)
        self.repository.add_retained_accounts(
            account["account_id"] for account in restored
        )
        return {
            "ok": True,
            "restored_count": len(restored),
            "message": f"已還原 {len(restored)} 個帳號至保留名單。",
        }

    async def _save_selection(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_ids = payload.get("account_ids", [])
        account_ids = raw_ids if isinstance(raw_ids, list) else []
        known_ids = {account["account_id"] for account in self.repository.list_accounts()}
        selected = [str(item) for item in account_ids if str(item) in known_ids]
        self.repository.save_selection(selected)
        return {
            "ok": True,
            "selected_count": len(selected),
            "accounts": self.repository.list_accounts(),
            "message": f"已儲存 {len(selected)} 個下載帳號。",
        }

    async def _scan_posts(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._mark_user_opened()
        raw_ids = payload.get("account_ids", [])
        requested_ids = raw_ids if isinstance(raw_ids, list) else []
        accounts = (
            self.repository.get_accounts(requested_ids)
            if requested_ids
            else self.repository.list_accounts(selected_only=True)
        )
        if not accounts:
            return {"ok": False, "message": "請先勾選要瀏覽貼文的帳號"}

        try:
            limit_per_account = int(payload.get("limit_per_account", 12) or 12)
        except (TypeError, ValueError):
            limit_per_account = 12
        limit_per_account = max(1, min(30, limit_per_account))
        inspect_existing = bool(payload.get("inspect_existing", False))
        account_ids = [str(account["account_id"]) for account in accounts]
        scan_job_id = uuid.uuid4().hex[:12]
        self._mark_browser_session_requested()
        self.repository.queue_account_scans(account_ids)
        self.repository.create_post_scan_job(
            scan_job_id,
            account_ids,
            limit_per_account,
            inspect_existing,
        )
        self._schedule_post_scan_job(scan_job_id)
        return {
            "ok": True,
            "scan_job_id": scan_job_id,
            "status": "queued",
            "account_count": len(account_ids),
            "message": f"貼文索引已排程：{len(account_ids)} 個帳號，每帳號最多 {limit_per_account} 篇。",
        }

    async def _cancel_post_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        scan_job_id = str(payload.get("scan_job_id", "")).strip()
        job = self.repository.get_post_scan_job(scan_job_id)
        if job is None:
            return {"ok": False, "message": "找不到貼文索引工作"}
        if job["status"] in {"completed", "failed", "cancelled"}:
            return {"ok": True, "message": "貼文索引工作已經結束"}
        self._cancelled_post_scan_jobs.add(scan_job_id)
        self.repository.update_post_scan_job(
            scan_job_id,
            status="cancelled",
            message="使用者已取消貼文索引",
            finished_at=self._now(),
        )
        task = self._post_scan_tasks.get(scan_job_id)
        if task is not None and not task.done():
            task.cancel()
        return {"ok": True, "message": "已取消貼文索引"}

    async def _create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._mark_user_opened()
        raw_ids = payload.get("account_ids", [])
        requested_ids = raw_ids if isinstance(raw_ids, list) else []
        accounts = self.repository.get_accounts(requested_ids)
        account_ids = [str(account["account_id"]) for account in accounts]
        if not account_ids:
            return {"ok": False, "message": "請至少勾選一個追蹤帳號"}

        preview_only = bool(payload.get("preview_only", False))
        destination_text = str(payload.get("destination", "")).strip()
        try:
            destination, _health = self._validate_destination(
                destination_text,
                required=not preview_only,
            )
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        if not preview_only:
            if destination is None:
                return {"ok": False, "message": "請先選擇可寫入的下載資料夾"}
            self.repository.set_setting("destination", str(destination.resolve()))

        conditions = normalize_conditions(payload.get("conditions", {}))
        self.repository.save_selection(account_ids)
        job_id = uuid.uuid4().hex[:12]
        self._mark_browser_session_requested()
        self.repository.create_job(
            job_id,
            account_ids,
            conditions,
            str(destination.resolve()) if destination is not None else "",
            preview_only,
        )
        self._schedule_job(job_id)
        return {
            "ok": True,
            "job_id": job_id,
            "status": "queued",
            "message": "預覽工作已排入背景佇列" if preview_only else "下載工作已排入背景佇列",
        }

    async def _create_link_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._mark_user_opened()
        links = self._payload_links(payload.get("links", []))
        targets = self._normalize_link_targets(links)
        if not targets:
            return {
                "ok": False,
                "message": "請貼上 Instagram / X 的貼文、Reel、Story 或 status 連結。",
            }

        preview_only = bool(payload.get("preview_only", False))
        destination_text = str(payload.get("destination", "")).strip()
        try:
            destination, _health = self._validate_destination(
                destination_text,
                required=not preview_only,
            )
        except ValueError as exc:
            return {"ok": False, "message": str(exc)}
        if not preview_only:
            if destination is None:
                return {"ok": False, "message": "請先選擇可寫入的下載資料夾。"}
            self.repository.set_setting("destination", str(destination.resolve()))

        conditions = normalize_conditions(payload.get("conditions", {}))
        conditions["source"] = "links"
        conditions["link_targets"] = targets
        job_id = uuid.uuid4().hex[:12]
        self._mark_browser_session_requested()
        self.repository.create_link_job(
            job_id,
            [str(target["url"]) for target in targets],
            conditions,
            str(destination.resolve()) if destination is not None else "",
            preview_only,
        )
        self._schedule_job(job_id)
        return {
            "ok": True,
            "job_id": job_id,
            "status": "queued",
            "target_count": len(targets),
            "message": (
                f"連結預覽已排程：{len(targets)} 個目標"
                if preview_only
                else f"連結快存已排程：{len(targets)} 個目標"
            ),
        }

    async def _retry_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("job_id", "")).strip()
        job = self.repository.get_job(job_id)
        if job is None:
            return {"ok": False, "message": "找不到要重跑的下載工作"}
        if job["status"] in {"queued", "running"}:
            return {"ok": False, "message": "工作仍在佇列或執行中，不需要重跑"}

        preview_only = bool(job.get("preview_only", False))
        destination_text = str(job.get("destination", "")).strip()
        try:
            destination, _health = self._validate_destination(
                destination_text,
                required=not preview_only,
            )
        except ValueError as exc:
            return {"ok": False, "message": f"無法重跑：{exc}"}

        new_job_id = uuid.uuid4().hex[:12]
        conditions = dict(job.get("conditions", {}))
        account_ids = [str(item) for item in job.get("account_ids", [])]
        destination_value = str(destination.resolve()) if destination is not None else ""
        if str(conditions.get("source", "")) == "links":
            self.repository.create_link_job(
                new_job_id,
                account_ids,
                conditions,
                destination_value,
                preview_only,
            )
        else:
            accounts = self.repository.get_accounts(account_ids)
            retained_ids = [str(account["account_id"]) for account in accounts]
            if not retained_ids:
                return {"ok": False, "message": "原工作帳號已不存在，無法重跑"}
            self.repository.create_job(
                new_job_id,
                retained_ids,
                conditions,
                destination_value,
                preview_only,
            )
        self._mark_browser_session_requested()
        self._schedule_job(new_job_id)
        return {
            "ok": True,
            "job_id": new_job_id,
            "status": "queued",
            "message": f"已建立重跑工作：{new_job_id}",
        }

    async def _cancel_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = str(payload.get("job_id", "")).strip()
        job = self.repository.get_job(job_id)
        if job is None:
            return {"ok": False, "message": "找不到下載工作"}
        if job["status"] in {"completed", "failed", "cancelled"}:
            return {"ok": True, "message": "工作已經結束"}
        self._cancelled_jobs.add(job_id)
        self.repository.update_job(job_id, status="cancelled", message="使用者已取消", finished_at=self._now())
        task = self._job_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
        return {"ok": True, "message": "已取消工作"}

    def _schedule_job(self, job_id: str) -> None:
        existing = self._job_tasks.get(job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._run_job(job_id))
        self._job_tasks[job_id] = task
        task.add_done_callback(lambda _task, key=job_id: self._job_tasks.pop(key, None))

    def _schedule_post_scan_job(self, scan_job_id: str) -> None:
        existing = self._post_scan_tasks.get(scan_job_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.create_task(self._run_post_scan_job(scan_job_id))
        self._post_scan_tasks[scan_job_id] = task
        task.add_done_callback(
            lambda _task, key=scan_job_id: self._post_scan_tasks.pop(key, None)
        )

    async def _run_post_scan_job(self, scan_job_id: str) -> None:
        job = self.repository.get_post_scan_job(scan_job_id)
        if job is None or job["status"] == "cancelled":
            return
        counters = {
            "discovered": int(job.get("discovered", 0) or 0),
            "inspected": int(job.get("inspected", 0) or 0),
            "skipped_existing": int(job.get("skipped_existing", 0) or 0),
            "failed": int(job.get("failed", 0) or 0),
        }
        account_ids = [
            str(item)
            for item in job.get("account_ids", [])
            if str(item).strip()
        ]
        accounts = self.repository.get_accounts(account_ids)
        self.repository.update_post_scan_job(
            scan_job_id,
            status="running",
            progress_total=len(accounts),
            message="正在準備貼文索引",
            started_at=self._now(),
            **counters,
        )

        try:
            for account_index, account in enumerate(accounts, start=1):
                if self._is_post_scan_cancelled(scan_job_id):
                    self.repository.update_post_scan_job(
                        scan_job_id,
                        status="cancelled",
                        message="貼文索引已取消",
                        finished_at=self._now(),
                        **counters,
                    )
                    return
                handle = str(account.get("handle", "")).strip()
                self.repository.update_post_scan_job(
                    scan_job_id,
                    progress_current=account_index - 1,
                    message=f"正在索引 @{handle or account.get('account_id')}",
                    **counters,
                )
                try:
                    result = await self._index_account_posts(
                        account,
                        int(job.get("limit_per_account", 12) or 12),
                        bool(job.get("inspect_existing", False)),
                    )
                    for key in counters:
                        counters[key] += int(result.get(key, 0) or 0)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    counters["failed"] += 1
                    self.repository.mark_account_scan_failure(
                        str(account.get("account_id", "")),
                        str(exc),
                    )
                self.repository.update_post_scan_job(
                    scan_job_id,
                    progress_current=account_index,
                    message=f"完成 @{handle or account.get('account_id')}",
                    **counters,
                )

            if self._is_post_scan_cancelled(scan_job_id):
                self.repository.update_post_scan_job(
                    scan_job_id,
                    status="cancelled",
                    message="貼文索引已取消",
                    finished_at=self._now(),
                    **counters,
                )
                return
            self.repository.update_post_scan_job(
                scan_job_id,
                status="completed",
                message=(
                    f"貼文索引完成：發現 {counters['discovered']}，"
                    f"檢查 {counters['inspected']}，略過既有 {counters['skipped_existing']}，"
                    f"失敗 {counters['failed']}"
                ),
                finished_at=self._now(),
                **counters,
            )
        except asyncio.CancelledError:
            if self._is_post_scan_cancelled(scan_job_id):
                self.repository.update_post_scan_job(
                    scan_job_id,
                    status="cancelled",
                    message="貼文索引已取消",
                    finished_at=self._now(),
                    **counters,
                )
                return
            self.repository.update_post_scan_job(
                scan_job_id,
                status="queued",
                message="貼文索引中斷，等待重新執行",
                **counters,
            )
            raise
        except Exception as exc:
            self.repository.update_post_scan_job(
                scan_job_id,
                status="failed",
                message=f"貼文索引失敗：{exc}",
                finished_at=self._now(),
                **counters,
            )
        finally:
            self._cancelled_post_scan_jobs.discard(scan_job_id)

    async def _index_account_posts(
        self,
        account: dict[str, Any],
        limit_per_account: int,
        inspect_existing: bool,
    ) -> dict[str, int]:
        platform = str(account.get("platform", "")).strip()
        definition = PLATFORMS.get(platform)
        if definition is None:
            raise RuntimeError("不支援的平台")
        account_id = str(account.get("account_id", "")).strip()
        schedule = self.repository.get_account_scan_schedule(account_id) or {}
        last_seen_post_url = str(schedule.get("last_seen_post_url", "")).strip()
        newest_post_url = ""
        newest_published_at = ""
        counters = {
            "discovered": 0,
            "inspected": 0,
            "skipped_existing": 0,
            "failed": 0,
        }

        async with self._post_scan_locks[platform]:
            self.repository.mark_account_scan_started(account_id)
            adapter = get_adapter(platform)
            page = await self.session.ensure_external_page(
                f"vaultly:posts:{platform}",
                "",
                (),
            )
            posts = await adapter.discover_posts(
                page,
                str(account.get("profile_url", "")),
                max(1, min(30, limit_per_account)),
            )
            for post in posts:
                if not isinstance(post, dict):
                    continue
                post_url = str(post.get("post_url", "")).strip()
                if not post_url:
                    continue
                if not newest_post_url:
                    newest_post_url = post_url
                    newest_published_at = str(post.get("published_at", "")).strip()
                if post_url == last_seen_post_url and not inspect_existing:
                    break
                counters["discovered"] += 1
                existing = self.repository.get_post_by_url(platform, post_url)
                self._store_post_snapshot(
                    account,
                    post,
                    definition,
                    scan_status="discovered",
                    media_items=None,
                )
                if (
                    existing is not None
                    and str(existing.get("scan_status", "")) in {"ready", "no_media"}
                    and not inspect_existing
                ):
                    counters["skipped_existing"] += 1
                    continue
                try:
                    inspected = await adapter.inspect_post(page, post)
                    self._store_post_snapshot(account, inspected, definition)
                    counters["inspected"] += 1
                except Exception as exc:
                    counters["failed"] += 1
                    self._store_post_snapshot(
                        account,
                        post,
                        definition,
                        scan_status="error",
                        last_error=str(exc),
                        media_items=None,
                    )
            self.repository.mark_account_scan_success(
                account_id,
                newest_post_url,
                newest_published_at,
                counters["discovered"],
                counters["inspected"],
            )
        return counters

    async def _run_job(self, job_id: str) -> None:
        async with self._job_lock:
            job = self.repository.get_job(job_id)
            if job is None or job["status"] == "cancelled":
                return
            self.repository.update_job(
                job_id,
                status="running",
                message="正在準備共用登入瀏覽器",
                started_at=self._now(),
            )
            counters = {"matched": 0, "downloaded": 0, "skipped": 0, "failed": 0}
            try:
                if str(job.get("conditions", {}).get("source", "")) == "links":
                    await self._run_link_job(job, counters)
                    return
                accounts = self.repository.get_accounts(job["account_ids"])
                for account_index, account in enumerate(accounts, start=1):
                    if self._is_cancelled(job_id):
                        return
                    self.repository.update_job(
                        job_id,
                        progress_current=account_index - 1,
                        message=f"掃描 {account['platform']} / @{account['handle']}",
                        **counters,
                    )
                    try:
                        await self._process_account(job, account, counters)
                        account_message = f"完成 @{account['handle']}"
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        counters["failed"] += 1
                        account_message = (
                            f"@{account['handle']} 失敗：{self._short_error(exc)}"
                        )
                    self.repository.update_job(
                        job_id,
                        progress_current=account_index,
                        message=account_message,
                        **counters,
                    )

                if self._is_cancelled(job_id):
                    return
                message = (
                    f"預覽完成：符合 {counters['matched']} 個媒體"
                    if job["preview_only"]
                    else f"下載完成：成功 {counters['downloaded']}、略過 {counters['skipped']}、失敗 {counters['failed']}"
                )
                self.repository.update_job(
                    job_id,
                    status="completed",
                    message=message,
                    finished_at=self._now(),
                    **counters,
                )
            except asyncio.CancelledError:
                if self._is_cancelled(job_id):
                    self.repository.update_job(
                        job_id,
                        status="cancelled",
                        message="使用者已取消",
                        finished_at=self._now(),
                        **counters,
                    )
                    return
                self.repository.update_job(
                    job_id,
                    status="queued",
                    message="主程式關閉，工作會在下次啟動後繼續",
                    **counters,
                )
                raise
            except Exception as exc:
                self.repository.update_job(
                    job_id,
                    status="failed",
                    message=f"工作失敗：{exc}",
                    finished_at=self._now(),
                    **counters,
                )
            finally:
                self._cancelled_jobs.discard(job_id)

    async def _run_link_job(
        self,
        job: dict[str, Any],
        counters: dict[str, int],
    ) -> None:
        job_id = str(job["job_id"])
        targets = job.get("conditions", {}).get("link_targets", [])
        if not isinstance(targets, list):
            targets = []
        if not targets:
            targets = self._normalize_link_targets(
                [str(item) for item in job.get("account_ids", [])]
            )
        self.repository.update_job(job_id, progress_total=len(targets))

        for target_index, target in enumerate(targets, start=1):
            if self._is_cancelled(job_id):
                return
            if not isinstance(target, dict):
                counters["failed"] += 1
                self.repository.update_job(
                    job_id,
                    progress_current=target_index,
                    message=f"連結 {target_index}/{len(targets)} 格式不正確",
                    **counters,
                )
                continue
            platform = str(target.get("platform", "")).strip()
            url = str(target.get("url", "")).strip()
            self.repository.update_job(
                job_id,
                progress_current=target_index - 1,
                message=f"解析連結 {target_index}/{len(targets)}",
                **counters,
            )
            try:
                await self._process_link_target(job, platform, url, counters)
                link_message = f"完成連結 {target_index}/{len(targets)}"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                counters["failed"] += 1
                link_message = (
                    f"連結 {target_index}/{len(targets)} 失敗：{self._short_error(exc)}"
                )
            self.repository.update_job(
                job_id,
                progress_current=target_index,
                message=link_message,
                **counters,
            )

        if self._is_cancelled(job_id):
            return
        message = (
            f"連結預覽完成：找到 {counters['matched']} 個媒體"
            if job["preview_only"]
            else (
                "連結快存完成："
                f"下載 {counters['downloaded']}、略過 {counters['skipped']}、"
                f"失敗 {counters['failed']}"
            )
        )
        self.repository.update_job(
            job_id,
            status="completed",
            message=message,
            finished_at=self._now(),
            **counters,
        )

    def _store_post_snapshot(
        self,
        account: dict[str, Any],
        post: dict[str, Any],
        definition: Any,
        scan_status: str = "",
        last_error: str = "",
        media_items: list[dict[str, Any]] | None = None,
    ) -> str:
        normalized_media = media_items
        if normalized_media is None and isinstance(post.get("media"), list):
            normalized_media = self._post_media_items(post, definition)
        status = scan_status
        if not status:
            if normalized_media is None:
                status = "discovered"
            else:
                status = "ready" if normalized_media else "no_media"
        return self.repository.upsert_post(
            account,
            post,
            media_items=normalized_media,
            scan_status=status,
            last_error=last_error,
        )

    def _post_media_items(
        self,
        post: dict[str, Any],
        definition: Any,
    ) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        raw_media = post.get("media", [])
        if not isinstance(raw_media, list):
            return output
        for media in raw_media:
            if not isinstance(media, dict):
                continue
            media_type = str(media.get("media_type", "")).strip()
            source_urls = self._allowed_media_sources(media, definition.media_hosts)
            if not media_type or not source_urls:
                continue
            thumbnail_url = str(media.get("thumbnail_url", "")).strip()
            if not thumbnail_url and media_type == "photo":
                thumbnail_url = source_urls[0]
            output.append(
                {
                    **media,
                    "media_type": media_type,
                    "source_url": source_urls[0],
                    "fallback_urls": source_urls[1:],
                    "thumbnail_url": thumbnail_url,
                }
            )
        return output

    async def _process_link_target(
        self,
        job: dict[str, Any],
        platform: str,
        url: str,
        counters: dict[str, int],
    ) -> None:
        definition = PLATFORMS.get(platform)
        if definition is None or not url:
            raise RuntimeError("unsupported link target")
        adapter = get_adapter(platform)
        page = await self.session.ensure_external_page(
            f"vaultly:link:{platform}",
            "",
            (),
        )
        inspected = await adapter.inspect_post(
            page,
            {"post_url": url, "text": "", "published_at": ""},
        )
        account = self._link_account(platform, url)
        self._store_post_snapshot(account, inspected, definition)
        matches, _reason = post_matches_conditions(inspected, job["conditions"])
        if not matches:
            counters["skipped"] += 1
            return
        media_items = [
            media
            for media in inspected.get("media", [])
            if isinstance(media, dict)
            and media_matches_conditions(media, job["conditions"])
        ]
        if not media_items:
            counters["skipped"] += 1
            return

        matched_for_link = 0
        for media_index, media in enumerate(media_items):
            media_type = str(media.get("media_type", "")).strip()
            source_urls = self._allowed_media_sources(media, definition.media_hosts)
            if not source_urls:
                counters["skipped"] += 1
                continue
            media = {
                **media,
                "source_url": source_urls[0],
                "fallback_urls": source_urls[1:],
            }
            dedupe_key = self._dedupe_key(account, inspected, media_type, media_index)
            if job["conditions"]["skip_downloaded"] and self._has_valid_download(
                dedupe_key,
                media_type,
            ):
                counters["skipped"] += 1
                continue
            counters["matched"] += 1
            matched_for_link += 1
            if job["preview_only"]:
                continue
            try:
                await self._download_media(
                    page,
                    job,
                    account,
                    inspected,
                    media,
                    dedupe_key,
                )
                counters["downloaded"] += 1
            except Exception as exc:
                counters["failed"] += 1
                self.repository.update_job(
                    str(job["job_id"]),
                    message=f"連結媒體下載失敗：{self._short_error(exc)}",
                    **counters,
                )
    @staticmethod
    def _link_account(platform: str, url: str) -> dict[str, str]:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.split("/") if part]
        handle = f"{platform}_link"
        if platform == "instagram" and len(parts) >= 2 and parts[0].casefold() == "stories":
            handle = parts[1].lstrip("@") or handle
        elif platform == "x" and parts and parts[0].casefold() != "i":
            handle = parts[0].lstrip("@") or handle
        account_hash = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        return {
            "account_id": f"{platform}:link:{account_hash}",
            "platform": platform,
            "handle": handle,
            "display_name": "",
            "profile_url": url,
            "avatar_url": "",
        }

    async def _process_account(
        self,
        job: dict[str, Any],
        account: dict[str, Any],
        counters: dict[str, int],
    ) -> None:
        platform = str(account["platform"])
        adapter = get_adapter(platform)
        definition = PLATFORMS[platform]
        page = await self.session.ensure_external_page(
            f"vaultly:download:{platform}",
            "",
            (),
        )
        maximum = int(job["conditions"]["max_items_per_account"])
        posts = await adapter.discover_posts(page, str(account["profile_url"]), maximum * 3)
        for post in posts:
            if isinstance(post, dict):
                self._store_post_snapshot(
                    account,
                    post,
                    definition,
                    scan_status="discovered",
                    media_items=None,
                )
        matched_for_account = 0

        for post in posts:
            if self._is_cancelled(str(job["job_id"])) or matched_for_account >= maximum:
                return
            inspected = await adapter.inspect_post(page, post)
            self._store_post_snapshot(account, inspected, definition)
            matches, _reason = post_matches_conditions(inspected, job["conditions"])
            if not matches:
                counters["skipped"] += 1
                continue

            media_items = [
                media
                for media in inspected.get("media", [])
                if isinstance(media, dict) and media_matches_conditions(media, job["conditions"])
            ]
            for media_index, media in enumerate(media_items):
                if matched_for_account >= maximum:
                    break
                media_type = str(media.get("media_type", "")).strip()
                source_urls = self._allowed_media_sources(media, definition.media_hosts)
                if not source_urls:
                    counters["skipped"] += 1
                    continue
                media = {
                    **media,
                    "source_url": source_urls[0],
                    "fallback_urls": source_urls[1:],
                }
                dedupe_key = self._dedupe_key(account, inspected, media_type, media_index)
                if job["conditions"]["skip_downloaded"] and self._has_valid_download(
                    dedupe_key,
                    media_type,
                ):
                    counters["skipped"] += 1
                    continue
                counters["matched"] += 1
                matched_for_account += 1
                if job["preview_only"]:
                    continue
                try:
                    await self._download_media(
                        page,
                        job,
                        account,
                        inspected,
                        media,
                        dedupe_key,
                    )
                    counters["downloaded"] += 1
                except Exception as exc:
                    counters["failed"] += 1
                    self.repository.update_job(
                        str(job["job_id"]),
                        message=(
                            f"@{account['handle']} 媒體下載失敗："
                            f"{self._short_error(exc)}"
                        ),
                        **counters,
                    )

    async def _download_media(
        self,
        page: Any,
        job: dict[str, Any],
        account: dict[str, Any],
        post: dict[str, Any],
        media: dict[str, Any],
        dedupe_key: str,
    ) -> None:
        media_type = str(media.get("media_type", "photo"))
        platform = str(account["platform"])
        definition = PLATFORMS[platform]
        source_urls = self._allowed_media_sources(media, definition.media_hosts)
        destination = Path(str(job["destination"]))
        errors: list[str] = []

        for source_url in source_urls:
            temp_path = destination / f".vaultly-{uuid.uuid4().hex}.part"
            try:
                content_type = await self._download_source_with_retries(
                    page,
                    source_url,
                    str(post["post_url"]),
                    media_type,
                    temp_path,
                )
                if not is_valid_media_file(temp_path, media_type):
                    raise RuntimeError("下載後的影片容器不完整或沒有可播放畫面")

                extension = extension_for_media(source_url, media_type, content_type)
                filename = build_download_filename(str(account["handle"]), extension)
                file_path = self._unique_download_path(destination / filename)
                temp_path.replace(file_path)
                digest = self._sha256_file(file_path)
                self.repository.record_download(
                    dedupe_key,
                    platform,
                    str(account["account_id"]),
                    str(post["post_url"]),
                    source_url,
                    str(file_path),
                    digest,
                )
                return
            except Exception as exc:
                errors.append(str(exc))
                with contextlib.suppress(OSError):
                    temp_path.unlink()

        message = errors[-1] if errors else "沒有可下載的完整媒體來源"
        raise RuntimeError(f"媒體下載失敗：{message}")

    async def _download_source_with_retries(
        self,
        page: Any,
        source_url: str,
        post_url: str,
        media_type: str,
        temp_path: Path,
    ) -> str:
        errors: list[str] = []
        for attempt in range(1, self.DOWNLOAD_RETRY_ATTEMPTS + 1):
            try:
                return (
                    await self._download_hls_media(page, source_url, post_url, temp_path)
                    if self._is_hls_url(source_url)
                    else await self._download_direct_media(
                        page,
                        source_url,
                        post_url,
                        media_type,
                        temp_path,
                    )
                )
            except Exception as exc:
                errors.append(f"第 {attempt} 次 {self._short_error(exc)}")
                with contextlib.suppress(OSError):
                    temp_path.unlink()
                if (
                    attempt >= self.DOWNLOAD_RETRY_ATTEMPTS
                    or not self._is_retryable_download_error(exc)
                ):
                    break
                await asyncio.sleep(self.DOWNLOAD_RETRY_BACKOFF_SECONDS * attempt)
        raise RuntimeError("；".join(errors) if errors else "沒有可下載的完整媒體來源")

    async def _download_direct_media(
        self,
        page: Any,
        source_url: str,
        post_url: str,
        media_type: str,
        temp_path: Path,
    ) -> str:
        headers = {
            "Accept": "video/*,*/*;q=0.8" if media_type == "video" else "image/*,*/*;q=0.8",
            "Referer": post_url,
        }
        cookie_header = await self._browser_cookie_header(page, source_url)
        if cookie_header:
            headers["Cookie"] = cookie_header

        response = await page.context.request.get(
            source_url,
            headers=headers,
            timeout=60_000,
        )
        if not response.ok:
            raise RuntimeError(f"媒體下載失敗：HTTP {response.status}")
        if response.status == 206:
            raise RuntimeError("平台只回傳部分媒體內容")
        content_length = int(response.headers.get("content-length", "0") or 0)
        if content_length > self.MAX_MEDIA_BYTES:
            raise RuntimeError("單一媒體超過 150 MB 安全限制")
        content = await response.body()
        if len(content) > self.MAX_MEDIA_BYTES:
            raise RuntimeError("單一媒體超過 150 MB 安全限制")
        content_type = response.headers.get("content-type", "")
        if not is_valid_media_payload(content, media_type, content_type):
            raise RuntimeError("下載內容不是可用的完整媒體檔")
        temp_path.write_bytes(content)
        return content_type

    async def _download_hls_media(
        self,
        page: Any,
        source_url: str,
        post_url: str,
        temp_path: Path,
    ) -> str:
        request_headers = f"Referer: {post_url}\r\n"
        cookie_header = await self._browser_cookie_header(page, source_url)
        if cookie_header:
            request_headers += f"Cookie: {cookie_header}\r\n"
        evaluate_method = getattr(page, "evaluate", None)
        if callable(evaluate_method):
            with contextlib.suppress(Exception):
                user_agent = str(await evaluate_method("() => navigator.userAgent")).strip()
                if user_agent:
                    request_headers += f"User-Agent: {user_agent}\r\n"

        command = [
            imageio_ffmpeg.get_ffmpeg_exe(),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-headers",
            request_headers,
            "-i",
            source_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-fs",
            str(self.MAX_MEDIA_BYTES),
            "-f",
            "mp4",
            "-y",
            str(temp_path),
        ]
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                command,
                capture_output=True,
                timeout=300,
                creationflags=creation_flags,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("串流影片合併超過 5 分鐘") from exc
        if completed.returncode != 0:
            stderr = completed.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(stderr[-500:] or "串流影片合併失敗")
        if not temp_path.is_file() or temp_path.stat().st_size <= 0:
            raise RuntimeError("串流影片合併後沒有產生檔案")
        if temp_path.stat().st_size >= self.MAX_MEDIA_BYTES:
            raise RuntimeError("單一媒體超過 150 MB 安全限制")
        return "video/mp4"

    @staticmethod
    async def _browser_cookie_header(page: Any, source_url: str) -> str:
        context = getattr(page, "context", None)
        cookies_method = getattr(context, "cookies", None)
        if not callable(cookies_method):
            return ""

        with contextlib.suppress(Exception):
            cookies = await cookies_method([source_url])
            return "; ".join(
                f"{cookie['name']}={cookie['value']}"
                for cookie in cookies
                if isinstance(cookie, dict) and cookie.get("name") and cookie.get("value")
            )
        return ""

    @staticmethod
    def _allowed_media_sources(
        media: dict[str, Any],
        allowed_hosts: tuple[str, ...],
    ) -> list[str]:
        raw_urls = [media.get("source_url", "")]
        fallback_urls = media.get("fallback_urls", [])
        if isinstance(fallback_urls, list):
            raw_urls.extend(fallback_urls)
        output: list[str] = []
        for raw_url in raw_urls:
            source_url = str(raw_url).strip()
            if (
                source_url
                and source_url not in output
                and is_allowed_media_url(source_url, allowed_hosts)
            ):
                output.append(source_url)
        return output

    @staticmethod
    def _is_hls_url(source_url: str) -> bool:
        return source_url.split("?", 1)[0].casefold().endswith(".m3u8")

    @staticmethod
    def _is_retryable_download_error(error: BaseException) -> bool:
        if isinstance(error, (TimeoutError, OSError)):
            return True
        message = str(error)
        if re.search(r"HTTP\s+(429|5\d\d)", message):
            return True
        retry_markers = (
            "timeout",
            "timed out",
            "temporarily unavailable",
            "connection reset",
            "connection aborted",
        )
        folded = message.casefold()
        return any(marker in folded for marker in retry_markers)

    @staticmethod
    def _unique_download_path(file_path: Path) -> Path:
        if not file_path.exists():
            return file_path
        for index in range(1, 1000):
            candidate = file_path.with_name(
                f"{file_path.stem}-{index}{file_path.suffix}"
            )
            if not candidate.exists():
                return candidate
        return file_path.with_name(
            f"{file_path.stem}-{uuid.uuid4().hex[:8]}{file_path.suffix}"
        )

    @staticmethod
    def _short_error(error: BaseException | str, limit: int = 180) -> str:
        if isinstance(error, BaseException):
            message = str(error).strip() or error.__class__.__name__
        else:
            message = str(error).strip()
        message = re.sub(r"\s+", " ", message)
        if len(message) <= limit:
            return message
        return f"{message[: max(0, limit - 3)]}..."

    @staticmethod
    def _sha256_file(file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancelled_jobs

    def _is_post_scan_cancelled(self, scan_job_id: str) -> bool:
        return scan_job_id in self._cancelled_post_scan_jobs

    def _has_valid_download(self, dedupe_key: str, media_type: str) -> bool:
        record = self.repository.get_download(dedupe_key)
        if record is None:
            return False
        return is_valid_media_file(Path(str(record["file_path"])), media_type)

    @staticmethod
    def _dedupe_key(
        account: dict[str, Any],
        post: dict[str, Any],
        media_type: str,
        media_index: int,
    ) -> str:
        raw = "|".join(
            [
                str(account["platform"]),
                str(account["account_id"]),
                str(post["post_url"]),
                media_type,
                str(media_index),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()
