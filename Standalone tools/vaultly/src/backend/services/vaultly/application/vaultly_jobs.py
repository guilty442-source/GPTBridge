from __future__ import annotations

import asyncio
import uuid
from typing import Any

from ..integration.adapters import PLATFORMS, get_adapter
from ..domain.rules import (
    media_matches_conditions,
    normalize_conditions,
    post_matches_conditions,
)


class VaultlyJobsMixin:
    """Download job creation, retry, cancellation, scheduling, and execution."""

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

    def _is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancelled_jobs
