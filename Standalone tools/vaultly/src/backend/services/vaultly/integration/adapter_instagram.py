from __future__ import annotations

from typing import Any

from .constants import HANDLE_PATTERNS, IGNORED_HANDLES
from .navigation_scripts import (
    AUTO_SCAN_CONTEXT_SCRIPT,
    INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT,
    INSTAGRAM_PROFILE_NAVIGATION_SCRIPT,
    OPEN_FOLLOWING_LIST_SCRIPT,
)


class InstagramNavigationMixin:
    """Mixin providing Instagram-specific DOM navigation helpers."""

    async def _is_instagram_unavailable_page(self, page: Any) -> bool:
        if self.definition.id != "instagram":
            return False
        try:
            return bool(await page.evaluate(INSTAGRAM_PAGE_UNAVAILABLE_SCRIPT))
        except Exception:
            return False

    async def _open_instagram_profile_from_nav(self, page: Any) -> bool:
        if self.definition.id != "instagram":
            return False
        try:
            opened = bool(await page.evaluate(INSTAGRAM_PROFILE_NAVIGATION_SCRIPT))
            if opened:
                await page.wait_for_timeout(1_500)
            return opened
        except Exception:
            return False

    async def _wait_for_following_ready(
        self,
        page: Any,
        attempts: int = 10,
        delay_ms: int = 750,
    ) -> dict[str, Any]:
        latest: dict[str, Any] = {}
        for attempt in range(max(1, attempts)):
            state = await page.evaluate(AUTO_SCAN_CONTEXT_SCRIPT, self.definition.id)
            latest = state if isinstance(state, dict) else {}
            if latest.get("ready") is True:
                self._set_prepare_status("ready", "追蹤名單已開啟")
                return latest
            if attempt + 1 < attempts:
                await page.wait_for_timeout(delay_ms)
        return latest

    async def _open_instagram_following(self, page: Any) -> bool:
        for _attempt in range(4):
            opened = await page.evaluate(OPEN_FOLLOWING_LIST_SCRIPT, self.definition.id)
            if opened:
                state = await self._wait_for_following_ready(
                    page,
                    attempts=12,
                    delay_ms=500,
                )
                if state.get("ready") is True:
                    return True
            await page.wait_for_timeout(1_000)
        return False

    async def _resolve_instagram_profile_from_settings(self, page: Any) -> dict[str, Any]:
        from .scan_scripts import INSTAGRAM_PROFILE_SETTINGS_SCRIPT

        if self.definition.id != "instagram":
            return {}
        try:
            await page.goto(
                "https://www.instagram.com/accounts/edit/",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            await page.wait_for_timeout(1_500)
            raw_handle = await page.evaluate(INSTAGRAM_PROFILE_SETTINGS_SCRIPT)
        except Exception:
            return {}

        handle = str(raw_handle or "").strip().lstrip("@")
        if not HANDLE_PATTERNS["instagram"].fullmatch(handle):
            return {}
        if handle.casefold() in IGNORED_HANDLES:
            return {}
        origin = "https://www.instagram.com"
        return {
            "logged_in": True,
            "ready": False,
            "target_url": f"{origin}/{handle}/",
            "following_url": f"{origin}/{handle}/following/",
            "message": "",
        }
