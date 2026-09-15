from __future__ import annotations

import asyncio
from time import monotonic
from typing import Any

from ..integration.adapters import PLATFORMS, get_adapter


class VaultlyScanMixin:
    """Platform scanning, auto-scan loop, and browser session lifecycle helpers."""

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
            failure = await self._scan_ready_or_failure(
                page, adapter, definition, platform
            )
            if failure is not None:
                return failure
            return await self._run_scan_collect(
                platform, automatic, page, adapter
            )

    async def _scan_ready_or_failure(
        self,
        page: Any,
        adapter: Any,
        definition: Any,
        platform: str,
    ) -> dict[str, Any] | None:
        prepare_scan = getattr(adapter, "prepare_following_scan", None)
        ready = await prepare_scan(page) if callable(prepare_scan) else True
        if ready:
            return None
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

    def _collect_scan_context(
        self, platform: str
    ) -> tuple[list[dict[str, Any]], set[str], set[str]]:
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
        return existing_accounts, retained_account_ids, manually_removed_account_ids

    @staticmethod
    def _filter_manually_removed(
        accounts: list[dict[str, Any]],
        manually_removed_account_ids: set[str],
    ) -> tuple[list[dict[str, Any]], int]:
        hidden_manual_removed_count = 0
        if not manually_removed_account_ids:
            return accounts, hidden_manual_removed_count
        visible_accounts = []
        for account in accounts:
            account_id = str(account.get("account_id", ""))
            if account_id in manually_removed_account_ids:
                hidden_manual_removed_count += 1
                continue
            visible_accounts.append(account)
        return visible_accounts, hidden_manual_removed_count

    async def _run_scan_collect(
        self,
        platform: str,
        automatic: bool,
        page: Any,
        adapter: Any,
    ) -> dict[str, Any]:
        existing_accounts, retained_ids, removed_ids = self._collect_scan_context(
            platform
        )
        accounts = await adapter.scan_following(
            page,
            filter_terms=self.repository.list_filter_terms(),
            retained_account_ids=sorted(retained_ids),
            reset_to_start=not (
                automatic and self._scan_continuations[platform]
            ),
        )
        accounts, hidden_manual_removed_count = self._filter_manually_removed(
            accounts, removed_ids
        )
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
            return self._instagram_empty_result(platform)
        filtered_ids = self._persist_scan_results(scan_stats, accounts)
        return self._scan_result(
            platform,
            automatic,
            scan_stats,
            accounts,
            filtered_ids,
            hidden_manual_removed_count,
            observed_count,
            reused_avatar_count,
            scan_completed,
        )

    def _instagram_empty_result(self, platform: str) -> dict[str, Any]:
        message = "Instagram 追蹤名單已開啟，但尚未讀到帳號；稍後會自動重試。"
        self._scan_continuations[platform] = False
        self._set_auto_scan_state(platform, "error", message)
        return {"ok": False, "platform": platform, "message": message}

    def _persist_scan_results(
        self, scan_stats: dict[str, Any], accounts: list[dict[str, Any]]
    ) -> list[str]:
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
        return filtered_ids

    def _scan_result(
        self,
        platform: str,
        automatic: bool,
        scan_stats: dict[str, Any],
        accounts: list[dict[str, Any]],
        filtered_ids: list[str],
        hidden_manual_removed_count: int,
        observed_count: int,
        reused_avatar_count: int,
        scan_completed: bool,
    ) -> dict[str, Any]:
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
