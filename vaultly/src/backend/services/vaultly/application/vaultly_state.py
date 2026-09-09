from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..integration.adapters import PLATFORMS


class VaultlyStateMixin:
    """State retrieval, report export, and automation summary helpers."""

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
            "workspace_path": str(self.project_root),
            "browser_profile_path": str(getattr(self.session, "shared_profile_dir", "")),
            "active_job_id": next(
                (job["job_id"] for job in jobs if job["status"] == "running"),
                "",
            ),
            "safety_notice": "只處理目前登入帳號可見的內容，不繞過私密權限、登入、平台限制或 DRM。",
        }

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
