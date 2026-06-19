from __future__ import annotations

import asyncio

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from managers.browser_session import BrowserSessionManager
from .base_provider import BaseAIProvider, SessionStatusEnum
from .selectors import GEMINI_SELECTORS

PLAYWRIGHT_ACTION_TIMEOUT_MS = 5000


class GeminiProvider(BaseAIProvider):
    def __init__(self, session_manager: BrowserSessionManager) -> None:
        super().__init__("gemini", session_manager)

    async def initialize_session(self) -> bool:
        self.page = self.session_manager.gemini_page
        if self.page is None:
            return False
        target_url = self.session_manager._provider_url(self.provider_name, "main")
        await self.page.goto(
            target_url,
            wait_until="domcontentloaded",
            timeout=15000,
        )
        return True

    async def enter_audit_mode(self) -> bool:
        """Legacy alias for the developer conversation URL."""
        if self.page is None:
            return False
        target_url = self.session_manager._provider_url(self.provider_name, "developer")
        await self.page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
        return True

    async def enter_main_mode(self) -> bool:
        """Switch back to the Gemini design conversation URL."""
        if self.page is None:
            return False
        target_url = self.session_manager._provider_url(self.provider_name, "main")
        await self.page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
        return True

    async def check_session_health(self) -> SessionStatusEnum:
        try:
            url = self.page.url
            title = await self.page.title()

            print(f"[Gemini] url={url}")
            print(f"[Gemini] title={title}")

            if "accounts.google.com" in url or "signin" in url:
                return SessionStatusEnum.UNAUTHENTICATED

            for selector in GEMINI_SELECTORS["login_check"]:
                count = await self.page.locator(selector).count()
                print(f"[Gemini] selector {selector} = {count}")
                if count > 0:
                    self.current_account = await self.detect_account_label()
                    return SessionStatusEnum.AUTHENTICATED

            return SessionStatusEnum.UNAUTHENTICATED

        except Exception as e:
            print(f"[Gemini] Session Health Check Error: {type(e).__name__} - {e}")
            return SessionStatusEnum.ERROR

    async def detect_account_label(self) -> str:
        if self.page is None:
            return ""
        selectors = [
            'a[aria-label*="Google"]',
            'button[aria-label*="Google"]',
            '[aria-label*="Account"]',
            '[aria-label*="\u5e33\u6236"]',
            '[aria-label*="\u5e33\u865f"]',
            'img[alt*="@"]',
        ]
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() == 0:
                    continue
                for attr in ("aria-label", "title", "alt"):
                    value = await locator.get_attribute(attr)
                    if value and value.strip():
                        return value.strip()[:200]
                text = (await locator.inner_text(timeout=1000)).strip()
                if text:
                    return text[:200]
            except Exception:
                continue
        return ""

    def _set_dispatch_error(self, message: str) -> None:
        self.last_dispatch_error = message
        print(f"[Gemini] dispatch detail: {message}")

    async def _visible_first(self, selectors: list[str]):
        if self.page is None:
            return None, ""
        for selector in selectors:
            locator = self.page.locator(selector)
            count = await locator.count()
            for index in range(min(count, 8)):
                candidate = locator.nth(index)
                try:
                    if await candidate.is_visible(timeout=700):
                        return candidate, selector
                except Exception:
                    continue
        return None, ""

    async def _type_prompt(self, input_box, prompt_text: str) -> None:
        if self.page is None:
            raise RuntimeError("page is not ready")
        await input_box.click(timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS)
        await input_box.focus(timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS)
        try:
            await self.page.keyboard.press("Control+A")
            await self.page.keyboard.press("Backspace")
            await self.page.keyboard.insert_text(prompt_text)
        except Exception:
            await input_box.fill(prompt_text, timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS)
        await self.page.wait_for_timeout(500)

    async def _click_send(self) -> tuple[bool, str]:
        if self.page is None:
            return False, ""
        for _ in range(12):
            send_button, selector = await self._visible_first(GEMINI_SELECTORS["send"])
            if send_button is not None:
                try:
                    if await send_button.is_enabled(timeout=700):
                        await send_button.click(timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS)
                        return True, selector
                except Exception:
                    pass
            await self.page.wait_for_timeout(250)
        return False, ""

    async def _dispatch_prompt_impl(self, prompt_text: str) -> bool:
        self.last_dispatch_error = ""
        if self.page is None:
            self._set_dispatch_error("page is not ready")
            return False

        health = await self.check_session_health()
        if health != SessionStatusEnum.AUTHENTICATED:
            self._set_dispatch_error(f"dispatch blocked: account state is {health.name}; url={self.page.url}")
            return False

        # 發送內容前先等待 3 秒
        await asyncio.sleep(3)

        try:
            input_box, input_selector = await self._visible_first(GEMINI_SELECTORS["input"])
            if input_box is None:
                self._set_dispatch_error(f"prompt input not found; url={self.page.url}")
                return False

            await self._type_prompt(input_box, prompt_text)

            sent, send_selector = await self._click_send()
            if sent:
                print(f"[Gemini] prompt sent input={input_selector} send={send_selector} url={self.page.url}")
                return True

            await self.page.keyboard.press("Enter")
            await self.page.wait_for_timeout(500)
            self._set_dispatch_error(f"send button not found; used Enter fallback; input={input_selector}; url={self.page.url}")
            return True

        except Exception as e:
            self._set_dispatch_error(f"dispatch failed: {type(e).__name__} - {e}; url={self.page.url}")
            return False

    async def dispatch_prompt(self, prompt_text: str, timeout_seconds: float = 20) -> bool:
        try:
            return await asyncio.wait_for(
                self._dispatch_prompt_impl(prompt_text),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._set_dispatch_error(f"dispatch timeout ({timeout_seconds}s); url={getattr(self.page, 'url', '')}")
            return False
        except PlaywrightTimeoutError as e:
            self._set_dispatch_error(f"playwright timeout: {e}; url={getattr(self.page, 'url', '')}")
            return False
        except Exception as e:
            self._set_dispatch_error(f"dispatch error: {e}; url={getattr(self.page, 'url', '')}")
            return False

    async def _capture_response_impl(self) -> str:
        if self.page is None:
            return ""

        for selector in GEMINI_SELECTORS["response"]:
            locator = self.page.locator(selector)
            if await locator.count() > 0:
                return await locator.last.inner_text(timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS)

        return ""

    async def capture_response(self, timeout_seconds: float = 120) -> str:
        try:
            return await asyncio.wait_for(
                self._capture_response_impl(),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            print(f"[Gemini] capture timeout ({timeout_seconds}s)")
            return ""
        except PlaywrightTimeoutError as e:
            print(f"[Gemini] capture playwright timeout: {e}")
            return ""
        except Exception as e:
            print(f"[Gemini] capture error: {e}")
            return ""

    async def recover(self) -> bool:
        try:
            self.page = await self.session_manager.ensure_gemini_page()
            return True
        except Exception as e:
            print(f"[Gemini] recover error: {e}")
            return False
