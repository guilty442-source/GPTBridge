from __future__ import annotations

import asyncio
import contextlib
import hashlib
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

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
    VERSION = "2.3.1"
    AUTO_SCAN_SUCCESS_INTERVAL_SECONDS = 30 * 60
    AUTO_SCAN_RETRY_INTERVAL_SECONDS = 30
    MAX_MEDIA_BYTES = 150 * 1024 * 1024
    COMMANDS = {
        "vaultly_get_state",
        "vaultly_open_platform",
        "vaultly_scan_following",
        "vaultly_add_filter_terms",
        "vaultly_remove_filter_terms",
        "vaultly_remove_accounts",
        "vaultly_restore_accounts",
        "vaultly_save_selection",
        "vaultly_create_job",
        "vaultly_cancel_job",
    }

    def __init__(self, project_root: Path, session: Any | None = None) -> None:
        self.project_root = project_root
        self._owns_session = session is None
        self.session = session or BrowserSessionManager(
            profile_name="vaultly",
            headless=True,
            profile_root=project_root / "runtime" / "browser-profiles",
        )
        self.repository = VaultlyRepository(project_root)
        self._job_lock = asyncio.Lock()
        self._job_tasks: dict[str, asyncio.Task[Any]] = {}
        self._pending_start_job_ids: set[str] = set()
        self._cancelled_jobs: set[str] = set()
        self._scan_locks = {platform: asyncio.Lock() for platform in PLATFORMS}
        self._scan_continuations = {platform: False for platform in PLATFORMS}
        self._user_opened = False
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
            if self._user_opened:
                self._schedule_job(job_id)
            else:
                self._pending_start_job_ids.add(job_id)
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
            "vaultly_open_platform": self._open_platform,
            "vaultly_scan_following": self._scan_following,
            "vaultly_add_filter_terms": self._add_filter_terms,
            "vaultly_remove_filter_terms": self._remove_filter_terms,
            "vaultly_remove_accounts": self._remove_accounts,
            "vaultly_restore_accounts": self._restore_accounts,
            "vaultly_save_selection": self._save_selection,
            "vaultly_create_job": self._create_job,
            "vaultly_cancel_job": self._cancel_job,
        }
        handler = handlers.get(command)
        if handler is None:
            return f"{command}_result", {"ok": False, "message": "不支援的 Vaultly 指令"}
        try:
            return f"{command}_result", await handler(payload)
        except Exception as exc:
            return f"{command}_result", {"ok": False, "message": str(exc)}

    async def _get_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        self._mark_user_opened()
        accounts = self.repository.list_accounts()
        jobs = self.repository.list_jobs()
        return {
            "ok": True,
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
            "removed_accounts": self.repository.list_removed_accounts(),
            "auto_scan": self._auto_scan_state,
            "jobs": jobs,
            "destination": self.repository.get_setting("destination", ""),
            "database_path": str(self.repository.db_path),
            "workspace_path": str(self.project_root / "platform_tools" / "vaultly"),
            "browser_profile_path": str(getattr(self.session, "shared_profile_dir", "")),
            "active_job_id": next(
                (job["job_id"] for job in jobs if job["status"] == "running"),
                "",
            ),
            "safety_notice": "只處理目前登入帳號可見的內容，不繞過私密權限、登入、平台限制或 DRM。",
        }

    async def _open_platform(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._mark_user_opened()
        platform = str(payload.get("platform", "")).strip()
        definition = PLATFORMS.get(platform)
        if definition is None:
            return {"ok": False, "message": "請選擇支援的平台"}
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
        if self._user_opened:
            return
        self._user_opened = True
        for job_id in sorted(self._pending_start_job_ids):
            self._schedule_job(job_id)
        self._pending_start_job_ids.clear()
        for platform, current in self._auto_scan_state.items():
            if current.get("status") == "waiting_login":
                self._set_auto_scan_state(
                    platform,
                    "waiting_login",
                    "等待登入後自動掃描",
                )
            self._auto_scan_next_due[platform] = 0.0

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
        destination = Path(destination_text).expanduser() if destination_text else None
        if not preview_only:
            if destination is None or not destination.is_dir():
                return {"ok": False, "message": "下載資料夾不存在，Vaultly 不會自動建立子資料夾"}
            self.repository.set_setting("destination", str(destination.resolve()))

        conditions = normalize_conditions(payload.get("conditions", {}))
        self.repository.save_selection(account_ids)
        job_id = uuid.uuid4().hex[:12]
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
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        counters["failed"] += 1
                    self.repository.update_job(
                        job_id,
                        progress_current=account_index,
                        message=f"完成 @{account['handle']}",
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
        matched_for_account = 0

        for post in posts:
            if self._is_cancelled(str(job["job_id"])) or matched_for_account >= maximum:
                return
            inspected = await adapter.inspect_post(page, post)
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
                except Exception:
                    counters["failed"] += 1

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
                content_type = (
                    await self._download_hls_media(page, source_url, str(post["post_url"]), temp_path)
                    if self._is_hls_url(source_url)
                    else await self._download_direct_media(
                        page,
                        source_url,
                        str(post["post_url"]),
                        media_type,
                        temp_path,
                    )
                )
                if not is_valid_media_file(temp_path, media_type):
                    raise RuntimeError("下載後的影片容器不完整或沒有可播放畫面")

                extension = extension_for_media(source_url, media_type, content_type)
                filename = build_download_filename(str(account["handle"]), extension)
                file_path = destination / filename
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
    def _sha256_file(file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def _is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancelled_jobs

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
