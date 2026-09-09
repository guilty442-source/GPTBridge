from __future__ import annotations

import asyncio
import base64
from typing import Any

from .constants import HANDLE_PATTERNS, IGNORED_HANDLES
from .platform_definition import PlatformDefinition
from .post_scripts import DISCOVER_POSTS_SCRIPT, INSPECT_POST_SCRIPT


class PlatformAdapterBase:
    """Base mixin providing account normalisation, avatar hydration, and post discovery."""

    definition: PlatformDefinition
    last_scan_stats: dict[str, Any]
    last_prepare_status: dict[str, str]

    def __init__(self, definition: PlatformDefinition) -> None:
        self.definition = definition
        self.last_scan_stats: dict[str, Any] = {
            "observed": 0,
            "accepted": 0,
            "filtered": 0,
            "filtered_account_ids": [],
            "filtered_accounts": [],
            "rounds": 0,
            "completed": False,
            "duration_ms": 0,
            "reset_to_start": False,
        }
        self.last_prepare_status: dict[str, str] = {
            "status": "waiting_login",
            "message": "等待登入後自動掃描",
        }

    def _normalize_scanned_account(self, account: Any) -> dict[str, Any] | None:
        if not isinstance(account, dict):
            return None
        handle = str(account.get("handle", "")).strip().lstrip("@")
        pattern = HANDLE_PATTERNS[self.definition.id]
        if not pattern.fullmatch(handle) or handle.casefold() in IGNORED_HANDLES:
            return None

        if self.definition.id == "instagram":
            profile_url = f"https://www.instagram.com/{handle}/"
        else:
            profile_url = f"https://x.com/{handle}"

        return {
            "account_id": f"{self.definition.id}:{handle.casefold()}",
            "platform": self.definition.id,
            "handle": handle,
            "display_name": str(account.get("display_name", "")).strip()[:120],
            "profile_url": profile_url,
            "avatar_url": str(account.get("avatar_url", "")).strip(),
            "context_text": str(account.get("context_text", "")).strip()[:500],
            "verified": bool(account.get("verified", False)),
        }

    @staticmethod
    def _merge_account(
        existing: dict[str, Any],
        incoming: dict[str, Any],
    ) -> None:
        for key in ("display_name", "avatar_url"):
            if not existing.get(key) and incoming.get(key):
                existing[key] = incoming[key]
        if incoming.get("verified") is True:
            existing["verified"] = True

    async def hydrate_avatar_urls(
        self,
        page: Any,
        accounts: list[dict[str, Any]],
    ) -> None:
        if self.definition.id != "instagram":
            return

        semaphore = asyncio.Semaphore(8)

        async def hydrate(account: dict[str, Any]) -> None:
            avatar_url = str(account.get("avatar_url", "")).strip()
            if not avatar_url.startswith("https://"):
                return
            async with semaphore:
                try:
                    response = await page.context.request.get(
                        avatar_url,
                        headers={
                            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                            "Referer": "https://www.instagram.com/",
                        },
                        timeout=15_000,
                    )
                    content_type = response.headers.get("content-type", "").split(";", 1)[0]
                    if not response.ok or not content_type.startswith("image/"):
                        return
                    content = await response.body()
                    if not content or len(content) > 512 * 1024:
                        return
                    encoded = base64.b64encode(content).decode("ascii")
                    account["avatar_url"] = f"data:{content_type};base64,{encoded}"
                except Exception:
                    return

        try:
            await asyncio.wait_for(
                asyncio.gather(*(hydrate(account) for account in accounts)),
                timeout=25,
            )
        except asyncio.TimeoutError:
            return

    def _set_prepare_status(self, status: str, message: str) -> None:
        self.last_prepare_status = {"status": status, "message": message}

    async def discover_posts(self, page: Any, profile_url: str, limit: int) -> list[dict[str, Any]]:
        await page.goto(profile_url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(1_500)
        discovered: dict[str, dict[str, Any]] = {}
        for _ in range(12):
            posts = await page.evaluate(DISCOVER_POSTS_SCRIPT, self.definition.id)
            for post in posts if isinstance(posts, list) else []:
                post_url = str(post.get("post_url", "")).strip()
                if post_url:
                    discovered[post_url] = {
                        "post_url": post_url,
                        "text": str(post.get("text", "")).strip(),
                        "published_at": str(post.get("published_at", "")).strip(),
                    }
            if len(discovered) >= limit:
                break
            await page.evaluate("() => window.scrollBy(0, Math.max(window.innerHeight * 0.85, 600))")
            await page.wait_for_timeout(850)
        return list(discovered.values())[:limit]

    async def inspect_post(self, page: Any, post: dict[str, Any]) -> dict[str, Any]:
        await page.goto(str(post["post_url"]), wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(1_300)
        inspected = await page.evaluate(INSPECT_POST_SCRIPT, self.definition.id)
        output = dict(post)
        if isinstance(inspected, dict):
            output.update(inspected)
        output["post_url"] = str(post["post_url"])
        return output
