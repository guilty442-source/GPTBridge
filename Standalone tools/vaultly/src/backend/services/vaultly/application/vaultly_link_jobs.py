from __future__ import annotations

import hashlib
from typing import Any
from urllib.parse import urlparse

from ..integration.adapters import PLATFORMS, get_adapter
from ..domain.rules import media_matches_conditions, post_matches_conditions


class VaultlyLinkJobsMixin:
    """Link-based download job execution and link target processing."""

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
