from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Locator, Page, async_playwright


VERIFICATION_MARKERS = (
    "captcha",
    "cloudflare",
    "verify you are human",
    "checking your browser",
    "are you a robot",
    "請驗證",
    "驗證你是人類",
)

INPUT_SELECTORS: dict[str, tuple[str, ...]] = {
    "chatgpt": ("#prompt-textarea", '[contenteditable="true"]', "textarea"),
    "claude": (
        'div[contenteditable="true"].ProseMirror',
        'div[contenteditable="true"]',
        "textarea",
    ),
    "gemini": (
        "rich-textarea .ql-editor",
        'div[contenteditable="true"]',
        "textarea",
    ),
    "grok": ("textarea", 'div[contenteditable="true"]'),
    "deepseek": ("textarea", 'div[contenteditable="true"]'),
    "perplexity": ("textarea", 'div[contenteditable="true"]'),
}

SEND_SELECTORS: dict[str, tuple[str, ...]] = {
    "chatgpt": (
        'button[data-testid="send-button"]',
        'button[aria-label*="Send" i]',
    ),
    "claude": (
        'button[aria-label*="Send" i]',
        'button[data-testid*="send" i]',
    ),
    "gemini": (
        "button.send-button",
        'button[aria-label*="Send" i]',
        'button[aria-label*="傳送"]',
    ),
    "grok": (
        'button[aria-label*="Submit" i]',
        'button[aria-label*="Send" i]',
        'button[type="submit"]',
    ),
    "deepseek": (
        'button[aria-label*="Send" i]',
        'button[type="submit"]',
    ),
    "perplexity": (
        'button[aria-label*="Submit" i]',
        'button[aria-label*="Send" i]',
        'button[type="submit"]',
    ),
}

RESPONSE_SELECTORS: dict[str, tuple[str, ...]] = {
    "chatgpt": ('[data-message-author-role="assistant"]',),
    "claude": (
        '[data-testid*="assistant" i]',
        ".font-claude-response",
        '[class*="assistant" i]',
    ),
    "gemini": ("model-response", ".model-response-text", '[class*="response" i]'),
    "grok": (
        '[data-testid*="assistant" i]',
        '[data-testid="message-bubble"]',
        '[class*="assistant" i]',
    ),
    "deepseek": (".ds-markdown", '[class*="assistant" i]', ".markdown"),
    "perplexity": (
        '[data-testid*="answer" i]',
        '[class*="answer" i]',
        ".prose",
    ),
}


class BrowserAutomationSession:
    """Drive provider websites in one visible, persistent Chrome profile."""

    RESPONSE_TIMEOUT_SECONDS = 120

    def __init__(self, profile_root: Path, chrome_executable: Path) -> None:
        self.profile_root = profile_root.resolve()
        self.chrome_executable = chrome_executable.resolve()
        self._playwright: Any | None = None
        self._context: BrowserContext | None = None
        self._pages: dict[str, Page] = {}
        self._init_lock = asyncio.Lock()
        self._page_lock = asyncio.Lock()
        self._recovery_profile_used = False

    async def open_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        try:
            page = await self._ensure_agent_page(agent)
            await page.bring_to_front()
            return {
                "ok": True,
                "url": page.url,
                "mode": "google-chrome-playwright-foreground",
                "automation": True,
                "foreground": True,
                "open_target": "shared-foreground-browser-tab",
                "shared_browser_context": True,
            }
        except Exception as exc:
            return self._browser_failure(exc)

    async def send_prompt(
        self, agent: dict[str, Any], prompt: str
    ) -> dict[str, Any]:
        provider = str(agent.get("provider") or "").strip().casefold()
        try:
            page = await self._ensure_agent_page(agent)
            await page.bring_to_front()
            input_box = await self._wait_for_input(
                page,
                INPUT_SELECTORS.get(provider, ("textarea", '[contenteditable="true"]')),
            )
            if input_box is None:
                error_code = (
                    "BROWSER_VERIFICATION_REQUIRED"
                    if await self._verification_required(page)
                    else "BROWSER_LOGIN_OR_INPUT_REQUIRED"
                )
                return self._waiting_result(
                    provider,
                    page,
                    error_code,
                    submitted=False,
                )

            before = await self._latest_response(page, provider)
            await input_box.click()
            await input_box.fill(prompt)
            sent_by = await self._submit(page, input_box, provider)
            if not sent_by:
                return self._waiting_result(
                    provider,
                    page,
                    "BROWSER_SEND_CONTROL_NOT_FOUND",
                    submitted=False,
                )

            content = await self._wait_for_response(page, provider, before)
            if not content:
                return self._waiting_result(
                    provider,
                    page,
                    "BROWSER_RESPONSE_CAPTURE_REQUIRED",
                    submitted=True,
                )
            return {
                "status": "completed",
                "provider": provider,
                "content": content,
                "error": "",
                "error_code": "",
                "transport": "google-chrome-playwright-foreground",
                "uses_api_key": False,
                "memory_candidates": [],
                "fallback": {
                    "used": False,
                    "browser_only": True,
                    "cross_provider_substitution": False,
                },
                "browser_handoff": {
                    "url": page.url,
                    "submitted": True,
                    "send_method": sent_by,
                    "response_captured": True,
                },
            }
        except Exception as exc:
            failure = self._browser_failure(exc)
            return {
                "status": "failed",
                "provider": provider,
                "content": "",
                "error": str(failure["message"]),
                "error_code": str(failure["error_code"]),
                "transport": "google-chrome-playwright-foreground",
                "uses_api_key": False,
                "memory_candidates": [],
                "fallback": {
                    "used": False,
                    "browser_only": True,
                    "cross_provider_substitution": False,
                },
            }

    async def shutdown(self) -> None:
        if self._context is not None:
            try:
                await self._context.close()
            except Exception:
                pass
        self._context = None
        self._pages = {}
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        self._playwright = None

    async def _ensure_context(self) -> BrowserContext:
        if self._context is not None:
            return self._context
        async with self._init_lock:
            if self._context is not None:
                return self._context
            self.profile_root.mkdir(parents=True, exist_ok=True)
            if self._playwright is None:
                self._playwright = await async_playwright().start()
            try:
                self._context = await self._launch_persistent_context(
                    self.profile_root
                )
            except Exception as exc:
                if not self._profile_is_in_use(exc):
                    raise
                recovery_profile = self.profile_root.with_name(
                    f"{self.profile_root.name}-recovery"
                )
                self._context = await self._launch_persistent_context(recovery_profile)
                self.profile_root = recovery_profile
                self._recovery_profile_used = True
            await self._context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            self._context.on("close", lambda _context=None: self._mark_context_closed())
            return self._context

    async def _launch_persistent_context(self, profile_root: Path) -> BrowserContext:
        profile_root.mkdir(parents=True, exist_ok=True)
        return await self._playwright.chromium.launch_persistent_context(
            user_data_dir=os.fspath(profile_root),
            executable_path=os.fspath(self.chrome_executable),
            headless=False,
            no_viewport=True,
            locale="zh-TW",
            ignore_default_args=("--enable-automation",),
            args=(
                "--start-maximized",
                "--disable-background-timer-throttling",
                "--disable-blink-features=AutomationControlled",
            ),
        )

    @staticmethod
    def _profile_is_in_use(exc: Exception) -> bool:
        message = str(exc).casefold()
        return any(
            marker in message
            for marker in (
                "user data directory is already in use",
                "processsingleton",
                "singletonlock",
                "profile in use",
            )
        )

    def _mark_context_closed(self) -> None:
        self._context = None
        self._pages = {}

    async def _ensure_agent_page(self, agent: dict[str, Any]) -> Page:
        agent_id = str(agent.get("agent_id") or "").strip()
        target_url = str(agent.get("home_url") or "").strip()
        if not agent_id or not target_url.startswith("https://"):
            raise ValueError("INVALID_BROWSER_AGENT")
        context = await self._ensure_context()
        async with self._page_lock:
            page = self._pages.get(agent_id)
            if page is None or page.is_closed():
                page = await context.new_page()
                self._pages[agent_id] = page
            if not self._same_conversation(page.url, target_url):
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60_000)
            return page

    @staticmethod
    def _same_conversation(current_url: str, target_url: str) -> bool:
        current = str(current_url or "").rstrip("/")
        target = target_url.rstrip("/")
        return bool(current) and (
            current == target
            or current.startswith(f"{target}?")
            or current.startswith(f"{target}/")
        )

    @staticmethod
    async def _find_visible(
        page: Page, selectors: tuple[str, ...]
    ) -> Locator | None:
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    if await candidate.is_visible() and await candidate.is_enabled():
                        return candidate
                except Exception:
                    continue
        return None

    async def _wait_for_input(
        self, page: Page, selectors: tuple[str, ...]
    ) -> Locator | None:
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            candidate = await self._find_visible(page, selectors)
            if candidate is not None:
                return candidate
            await page.wait_for_timeout(500)
        return None

    async def _submit(
        self, page: Page, input_box: Locator, provider: str
    ) -> str:
        await page.wait_for_timeout(250)
        button = await self._find_visible(
            page,
            SEND_SELECTORS.get(
                provider,
                ('button[type="submit"]', 'button[aria-label*="Send" i]'),
            ),
        )
        if button is not None:
            await button.click()
            return "button"
        try:
            await input_box.press("Enter")
            return "enter"
        except Exception:
            return ""

    async def _wait_for_response(
        self, page: Page, provider: str, before: str
    ) -> str:
        deadline = asyncio.get_running_loop().time() + self.RESPONSE_TIMEOUT_SECONDS
        last = ""
        stable_polls = 0
        while asyncio.get_running_loop().time() < deadline:
            await page.wait_for_timeout(1_000)
            current = await self._latest_response(page, provider)
            if current and current != before:
                if current == last:
                    stable_polls += 1
                else:
                    last = current
                    stable_polls = 0
                if stable_polls >= 2 and not await self._response_streaming(page):
                    return current
        return last if last and last != before and not await self._response_streaming(page) else ""

    async def _latest_response(self, page: Page, provider: str) -> str:
        selectors = RESPONSE_SELECTORS.get(provider, ())
        for selector in selectors:
            locator = page.locator(selector)
            count = await locator.count()
            for index in range(count - 1, -1, -1):
                candidate = locator.nth(index)
                try:
                    if not await candidate.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", (await candidate.inner_text()).strip())
                    if len(text) >= 2:
                        return text[:64_000]
                except Exception:
                    continue
        return ""

    @staticmethod
    async def _response_streaming(page: Page) -> bool:
        selectors = (
            'button[aria-label*="Stop" i]',
            'button[aria-label*="停止"]',
            'button[data-testid*="stop" i]',
        )
        for selector in selectors:
            locator = page.locator(selector)
            try:
                if await locator.count() and await locator.last.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    async def _verification_required(page: Page) -> bool:
        try:
            text = re.sub(
                r"\s+", " ", (await page.locator("body").inner_text()).casefold()
            )
        except Exception:
            return False
        return any(marker in text for marker in VERIFICATION_MARKERS)

    @staticmethod
    def _waiting_result(
        provider: str,
        page: Page,
        error_code: str,
        *,
        submitted: bool,
    ) -> dict[str, Any]:
        return {
            "status": "awaiting-user",
            "provider": provider,
            "content": "",
            "error": "",
            "error_code": error_code,
            "transport": "google-chrome-playwright-foreground",
            "uses_api_key": False,
            "memory_candidates": [],
            "fallback": {
                "used": False,
                "browser_only": True,
                "cross_provider_substitution": False,
            },
            "browser_handoff": {
                "url": page.url,
                "submitted": submitted,
                "response_captured": False,
            },
        }

    @staticmethod
    def _browser_failure(exc: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "message": f"BROWSER_AUTOMATION_FAILED: {type(exc).__name__}",
            "error_code": "BROWSER_AUTOMATION_FAILED",
        }
