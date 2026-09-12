from __future__ import annotations

from time import monotonic
from typing import Any, Iterable
from urllib.parse import urlencode

from .constants import INSTAGRAM_API_PAGE_SIZE
from .scan_scripts import (
    FOLLOWING_SCAN_SCRIPT,
    RESET_FOLLOWING_SCROLL_SCRIPT,
    SCROLL_SCRIPT,
)
from .star_candidate import is_star_candidate_account


class FollowingScanMixin:
    """Mixin providing following-list preparation and scanning logic."""

    async def _prepare_following_scan_with_dom(self, page: Any) -> bool:
        state = await self._wait_for_following_ready(page, attempts=3, delay_ms=1_000)
        if state.get("ready") is True and self.definition.id != "instagram":
            return True

        target_url = str(state.get("target_url", "")).strip()
        following_url = str(state.get("following_url", "")).strip()
        if self.definition.id == "instagram" and state.get("logged_in") is True:
            settings_state = await self._resolve_instagram_profile_from_settings(page)
            settings_target_url = str(settings_state.get("target_url", "")).strip()
            settings_following_url = str(
                settings_state.get("following_url", "")
            ).strip()
            if settings_target_url:
                target_url = settings_target_url
                following_url = settings_following_url
                state = settings_state
            elif await self._open_instagram_profile_from_nav(page):
                if await self._open_instagram_following(page):
                    return True
        if not target_url:
            if self.definition.id == "instagram" and state.get("logged_in") is True:
                fallback_state = await self._resolve_instagram_profile_from_settings(page)
                target_url = str(fallback_state.get("target_url", "")).strip()
                following_url = str(fallback_state.get("following_url", "")).strip()
                if target_url:
                    state = fallback_state
            if not target_url:
                message = str(state.get("message", "")).strip() or "等待登入後自動掃描"
                self._set_prepare_status(
                    "waiting_login" if state.get("logged_in") is not True else "error",
                    message,
                )
                return False

        if self.definition.id == "instagram":
            await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1_500)
            if await self._is_instagram_unavailable_page(page):
                fallback_state = await self._resolve_instagram_profile_from_settings(page)
                fallback_target_url = str(fallback_state.get("target_url", "")).strip()
                fallback_following_url = str(
                    fallback_state.get("following_url", "")
                ).strip()
                if fallback_target_url and fallback_target_url != target_url:
                    target_url = fallback_target_url
                    following_url = fallback_following_url
                    await page.goto(
                        target_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    await page.wait_for_timeout(1_500)
                else:
                    if await self._open_instagram_profile_from_nav(page):
                        if await self._open_instagram_following(page):
                            return True
                    await page.goto(self.definition.home_url, wait_until="domcontentloaded", timeout=60_000)
                    self._set_prepare_status(
                        "error",
                        "Instagram 個人頁無法使用，已回首頁重新等待自動解析。",
                    )
                    return False
            if await self._open_instagram_following(page):
                return True

            if following_url:
                await page.wait_for_timeout(1_500)
                if await self._open_instagram_following(page):
                    return True

            await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            await page.wait_for_timeout(1_500)
            if await self._open_instagram_following(page):
                return True

            self._set_prepare_status(
                "error",
                "Instagram 已登入，但無法自動開啟「追蹤中」名單；稍後會自動重試。",
            )
            return False

        await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
        state = await self._wait_for_following_ready(page)
        if state.get("ready") is True:
            return True
        self._set_prepare_status(
            "error",
            "X 已登入，但無法自動開啟追蹤名單；稍後會自動重試。",
        )
        return False

    async def prepare_following_scan(self, page: Any) -> bool:
        if self.definition.id == "instagram":
            cookie_state = await self._instagram_cookie_session_state(page)
            if cookie_state.get("logged_in") is True:
                self._set_prepare_status(
                    "ready",
                    "Instagram Cookie 登入可用，使用 Cookie 掃描追蹤名單",
                )
                return True

        return await self._prepare_following_scan_with_dom(page)

    async def _scan_instagram_following_with_cookies(
        self,
        page: Any,
        max_pages: int,
        filter_terms: Iterable[str],
        retained_account_ids: Iterable[str],
    ) -> list[dict[str, Any]] | None:
        session_state = await self._instagram_cookie_session_state(page)
        user_id = str(session_state.get("user_id", "")).strip()
        if not user_id:
            return None

        started_at = monotonic()
        discovered: dict[str, dict[str, Any]] = {}
        observed_ids: set[str] = set()
        filtered_accounts: dict[str, dict[str, Any]] = {}
        retained_ids = {
            str(account_id).strip()
            for account_id in retained_account_ids
            if str(account_id).strip()
        }
        rounds = 0
        completed_scan = False
        next_max_id = ""
        page_limit = max(1, min(120, max_pages))

        for _ in range(page_limit):
            rounds += 1
            query = {"count": INSTAGRAM_API_PAGE_SIZE}
            if next_max_id:
                query["max_id"] = next_max_id
            url = (
                f"https://www.instagram.com/api/v1/friendships/{user_id}/following/"
                f"?{urlencode(query)}"
            )
            payload = await self._instagram_request_json(page, url)
            if payload is None:
                return None
            users = payload.get("users", [])
            if not isinstance(users, list):
                return None
            for user in users:
                normalized = self._normalize_instagram_api_user(user)
                if normalized is None:
                    continue
                account_id = str(normalized["account_id"])
                observed_ids.add(account_id)
                accepted, reason = (
                    (True, "手動還原保留")
                    if account_id in retained_ids
                    else is_star_candidate_account(normalized, filter_terms)
                )
                if not accepted:
                    filtered_accounts[account_id] = {
                        **normalized,
                        "filter_reason": reason,
                        "filter_source": (
                            "manual" if reason.startswith("自訂篩選") else "automatic"
                        ),
                    }
                    discovered.pop(account_id, None)
                    continue
                if normalized.get("verified") is True:
                    filtered_accounts.pop(account_id, None)
                elif account_id in filtered_accounts:
                    continue
                existing = discovered.get(account_id)
                if existing is None:
                    discovered[account_id] = normalized
                else:
                    self._merge_account(existing, normalized)

            next_max_id = str(payload.get("next_max_id", "") or "").strip()
            if not next_max_id:
                completed_scan = True
                break

        self.last_scan_stats = {
            "observed": len(observed_ids),
            "accepted": len(discovered),
            "filtered": len(filtered_accounts),
            "filtered_account_ids": sorted(filtered_accounts),
            "filtered_accounts": sorted(
                filtered_accounts.values(),
                key=lambda account: str(account["handle"]).casefold(),
            ),
            "rounds": rounds,
            "completed": completed_scan,
            "duration_ms": round((monotonic() - started_at) * 1000),
            "reset_to_start": False,
            "method": "cookie_api",
        }
        return sorted(
            discovered.values(),
            key=lambda account: str(account["handle"]).casefold(),
        )

    async def scan_following(
        self,
        page: Any,
        max_scrolls: int = 160,
        filter_terms: Iterable[str] = (),
        retained_account_ids: Iterable[str] = (),
        reset_to_start: bool = True,
    ) -> list[dict[str, Any]]:
        if self.definition.id == "instagram":
            cookie_accounts = await self._scan_instagram_following_with_cookies(
                page,
                max_scrolls,
                filter_terms,
                retained_account_ids,
            )
            if cookie_accounts is not None:
                return cookie_accounts
            if not await self._prepare_following_scan_with_dom(page):
                return []

        started_at = monotonic()
        did_reset_to_start = False
        if reset_to_start:
            did_reset_to_start = bool(
                await page.evaluate(RESET_FOLLOWING_SCROLL_SCRIPT, self.definition.id)
            )
        if did_reset_to_start:
            await page.wait_for_timeout(350)
        discovered: dict[str, dict[str, Any]] = {}
        observed_ids: set[str] = set()
        filtered_accounts: dict[str, dict[str, Any]] = {}
        retained_ids = {
            str(account_id).strip()
            for account_id in retained_account_ids
            if str(account_id).strip()
        }
        stalled_rounds = 0
        end_rounds = 0
        last_position = -1
        rounds = 0
        completed_scan = False
        for _ in range(max(1, min(300, max_scrolls))):
            rounds += 1
            before_observed_count = len(observed_ids)
            scan_result = await page.evaluate(FOLLOWING_SCAN_SCRIPT, self.definition.id)
            if isinstance(scan_result, dict):
                accounts = scan_result.get("accounts", [])
                scroll_state = scan_result.get("scroll", {})
            else:
                accounts = scan_result
                scroll_state = await page.evaluate(SCROLL_SCRIPT, self.definition.id)
            for account in accounts if isinstance(accounts, list) else []:
                normalized = self._normalize_scanned_account(account)
                if normalized is None:
                    continue
                account_id = str(normalized["account_id"])
                observed_ids.add(account_id)
                accepted, reason = (
                    (True, "手動還原保留")
                    if account_id in retained_ids
                    else is_star_candidate_account(normalized, filter_terms)
                )
                if not accepted:
                    filtered_accounts[account_id] = {
                        **normalized,
                        "filter_reason": reason,
                        "filter_source": (
                            "manual" if reason.startswith("自訂篩選") else "automatic"
                        ),
                    }
                    discovered.pop(account_id, None)
                    continue
                if normalized.get("verified") is True:
                    filtered_accounts.pop(account_id, None)
                elif account_id in filtered_accounts:
                    continue
                existing = discovered.get(account_id)
                if existing is None:
                    discovered[account_id] = normalized
                else:
                    self._merge_account(existing, normalized)

            moved = bool(
                scroll_state.get("moved", False)
                if isinstance(scroll_state, dict)
                else scroll_state
            )
            position = int(
                scroll_state.get("position", last_position)
                if isinstance(scroll_state, dict)
                else last_position
            )
            maximum = int(
                scroll_state.get("maximum", position)
                if isinstance(scroll_state, dict)
                else position
            )
            at_end = bool(
                scroll_state.get("at_end", position >= maximum - 2)
                if isinstance(scroll_state, dict)
                else position >= maximum - 2
            )
            added_observed = len(observed_ids) - before_observed_count
            stalled_rounds = (
                stalled_rounds + 1
                if added_observed == 0 and (not moved or position == last_position)
                else 0
            )
            end_rounds = end_rounds + 1 if at_end and added_observed == 0 else 0
            last_position = position

            if end_rounds >= 3:
                completed_scan = True
                break
            if stalled_rounds >= 5:
                break
            await page.wait_for_timeout(
                550 if not moved else 400 if added_observed == 0 else 250
            )
        self.last_scan_stats = {
            "observed": len(observed_ids),
            "accepted": len(discovered),
            "filtered": len(filtered_accounts),
            "filtered_account_ids": sorted(filtered_accounts),
            "filtered_accounts": sorted(
                filtered_accounts.values(),
                key=lambda account: str(account["handle"]).casefold(),
            ),
            "rounds": rounds,
            "completed": completed_scan,
            "duration_ms": round((monotonic() - started_at) * 1000),
            "reset_to_start": did_reset_to_start,
        }
        return sorted(
            discovered.values(),
            key=lambda account: str(account["handle"]).casefold(),
        )
