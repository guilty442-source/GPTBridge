from __future__ import annotations

import asyncio
import uuid
from typing import Any

from ..integration.adapters import PLATFORMS, get_adapter


class VaultlyPostScanMixin:
    """Post scanning, post scan job execution, and post snapshot storage."""

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

    def _is_post_scan_cancelled(self, scan_job_id: str) -> bool:
        return scan_job_id in self._cancelled_post_scan_jobs
