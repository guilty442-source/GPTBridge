from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse, urlunparse


class VaultlyAccountsMixin:
    """Filter terms, account removal/restoration, and selection management."""

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
