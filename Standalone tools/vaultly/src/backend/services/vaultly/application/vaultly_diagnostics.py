from __future__ import annotations

from typing import Any

from ..integration.adapters import PLATFORMS


class VaultlyDiagnosticsMixin:
    """Diagnostics, health checks, and failure summaries for Vaultly."""

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
