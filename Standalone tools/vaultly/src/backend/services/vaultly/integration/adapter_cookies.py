from __future__ import annotations

import json
from typing import Any

from .constants import INSTAGRAM_WEB_APP_ID


class CookieMixin:
    """Mixin providing browser cookie helpers and Instagram API request methods."""

    @staticmethod
    async def _browser_cookies(page: Any, url: str) -> list[dict[str, Any]]:
        context = getattr(page, "context", None)
        cookies_method = getattr(context, "cookies", None)
        if not callable(cookies_method):
            return []

        try:
            cookies = await cookies_method([url])
        except Exception:
            return []
        return [cookie for cookie in cookies if isinstance(cookie, dict)]

    @classmethod
    async def _browser_cookie_map(cls, page: Any, url: str) -> dict[str, str]:
        cookies = await cls._browser_cookies(page, url)
        return {
            str(cookie.get("name", "")): str(cookie.get("value", ""))
            for cookie in cookies
            if cookie.get("name") and cookie.get("value")
        }

    @staticmethod
    def _cookie_header(cookies: dict[str, str]) -> str:
        return "; ".join(
            f"{name}={value}"
            for name, value in cookies.items()
            if name and value
        )

    async def _instagram_cookie_session_state(self, page: Any) -> dict[str, Any]:
        if self.definition.id != "instagram":
            return {}
        cookies = await self._browser_cookie_map(
            page,
            "https://www.instagram.com/",
        )
        if not cookies.get("sessionid") or not cookies.get("ds_user_id"):
            return {}
        return {
            "logged_in": True,
            "ready": True,
            "user_id": cookies["ds_user_id"],
            "message": "Instagram Cookie 登入可用",
        }

    async def _instagram_request_json(
        self,
        page: Any,
        url: str,
        *,
        referer: str = "https://www.instagram.com/",
    ) -> dict[str, Any] | None:
        context = getattr(page, "context", None)
        request = getattr(context, "request", None)
        get_method = getattr(request, "get", None)
        if not callable(get_method):
            return None

        cookies = await self._browser_cookie_map(page, "https://www.instagram.com/")
        if not cookies.get("sessionid"):
            return None
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Referer": referer,
            "X-ASBD-ID": "129477",
            "X-IG-App-ID": INSTAGRAM_WEB_APP_ID,
            "X-Requested-With": "XMLHttpRequest",
        }
        csrf_token = cookies.get("csrftoken")
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
        cookie_header = self._cookie_header(cookies)
        if cookie_header:
            headers["Cookie"] = cookie_header

        response = await get_method(url, headers=headers, timeout=45_000)
        if not getattr(response, "ok", False):
            return None

        json_method = getattr(response, "json", None)
        if callable(json_method):
            parsed = await json_method()
            return parsed if isinstance(parsed, dict) else None

        body_method = getattr(response, "body", None)
        if callable(body_method):
            raw_body = await body_method()
            parsed = json.loads(raw_body.decode("utf-8", errors="replace"))
            return parsed if isinstance(parsed, dict) else None
        return None

    def _normalize_instagram_api_user(self, user: Any) -> dict[str, Any] | None:
        if not isinstance(user, dict):
            return None
        handle = str(user.get("username", "")).strip()
        if not handle:
            return None
        return self._normalize_scanned_account(
            {
                "handle": handle,
                "display_name": user.get("full_name", ""),
                "avatar_url": user.get("profile_pic_url", ""),
                "context_text": " ".join(
                    str(value)
                    for value in (
                        user.get("full_name", ""),
                        user.get("username", ""),
                    )
                    if value
                ),
                "verified": bool(user.get("is_verified", False)),
            }
        )
