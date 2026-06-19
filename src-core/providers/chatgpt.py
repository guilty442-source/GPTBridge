from __future__ import annotations

import asyncio
import re

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from managers.browser_session import BrowserSessionManager
from .base_provider import BaseAIProvider, SessionStatusEnum
from .selectors import CHATGPT_SELECTORS

PLAYWRIGHT_ACTION_TIMEOUT_MS = 5000
EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


class ChatGPTProvider(BaseAIProvider):
    def __init__(self, session_manager: BrowserSessionManager) -> None:
        super().__init__("chatgpt", session_manager)

    async def initialize_session(self) -> bool:
        self.page = self.session_manager.chatgpt_page
        if self.page is None:
            return False
        target_url = self.session_manager._provider_url(self.provider_name, "main")
        await self.page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
        return True

    async def enter_audit_mode(self) -> bool:
        """Legacy alias for the developer conversation URL."""
        if self.page is None:
            return False
        target_url = self.session_manager._provider_url(self.provider_name, "developer")
        await self.page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
        return True

    async def enter_main_mode(self) -> bool:
        """Switch back to the ChatGPT design conversation URL."""
        if self.page is None:
            return False
        target_url = self.session_manager._provider_url(self.provider_name, "main")
        await self.page.goto(target_url, wait_until="domcontentloaded", timeout=15000)
        return True

    async def check_session_health(self) -> SessionStatusEnum:
        try:
            await self.page.wait_for_timeout(7000)

            url = self.page.url
            title = await self.page.title()

            print(f"[ChatGPT] url={url}")
            print(f"[ChatGPT] title={title}")

            if "auth" in url or "login" in url:
                return SessionStatusEnum.UNAUTHENTICATED

            if "\u8acb\u7a0d\u5019" in title or "Just a moment" in title:
                return SessionStatusEnum.UNAUTHENTICATED

            for selector in CHATGPT_SELECTORS["login_check"]:
                count = await self.page.locator(selector).count()
                print(f"[ChatGPT] selector {selector} = {count}")
                if count > 0:
                    self.current_account = await self.detect_account_label()
                    return SessionStatusEnum.AUTHENTICATED

            return SessionStatusEnum.UNAUTHENTICATED

        except Exception as e:
            print(f"[ChatGPT] health error: {e}")
            return SessionStatusEnum.ERROR

    async def detect_account_label(self) -> str:
        if self.page is None:
            return ""
        for detector in (
            self._detect_account_from_api,
            self._detect_account_from_dom,
            self._detect_account_from_storage,
            self._detect_account_from_profile_menu,
        ):
            try:
                label = await detector()
                label = self._clean_account_label(label)
                if label:
                    self.current_account = label
                    return label
            except Exception:
                continue
        return ""

    async def _detect_account_from_dom(self) -> str:
        if self.page is None:
            return ""
        selectors = [
            'button[data-testid="profile-button"]',
            'button[aria-label*="Profile"]',
            'button[aria-label*="profile"]',
            'button[aria-label*="account"]',
            'button[aria-label*="Account"]',
            'button[aria-label*="\u5e33\u6236"]',
            'button[aria-label*="\u5e33\u865f"]',
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
                    return text
            except Exception:
                continue
        return await self.page.evaluate(
            """
            () => {
              const values = [];
              const attrs = ["aria-label", "title", "alt", "data-testid"];
              for (const el of document.querySelectorAll("button,a,img,[aria-label],[title],[alt]")) {
                for (const attr of attrs) {
                  const value = el.getAttribute(attr);
                  if (value) values.push(value);
                }
                const text = (el.textContent || "").trim();
                if (text) values.push(text);
              }
              return values.join("\\n").slice(0, 12000);
            }
            """
        )

    async def _detect_account_from_storage(self) -> str:
        if self.page is None:
            return ""
        return await self.page.evaluate(
            """
            () => {
              const values = [];
              const scanValue = (value) => {
                if (!value) return;
                const text = String(value);
                if (text.includes("@") || text.toLowerCase().includes("email")) values.push(text);
              };
              for (const store of [localStorage, sessionStorage]) {
                for (let i = 0; i < store.length; i += 1) {
                  const key = store.key(i);
                  scanValue(key);
                  try { scanValue(store.getItem(key)); } catch {}
                }
              }
              return values.join("\\n").slice(0, 20000);
            }
            """
        )

    async def _detect_account_from_api(self) -> str:
        if self.page is None:
            return ""
        return await self.page.evaluate(
            """
            async () => {
              const endpoints = ["/backend-api/me", "/backend-api/accounts/check/v4-2023-04-27"];
              const collect = (value, output = []) => {
                if (value == null || output.length > 30) return output;
                if (typeof value === "string") {
                  if (value.includes("@") || /email|name|account/i.test(value)) output.push(value);
                  return output;
                }
                if (Array.isArray(value)) {
                  value.forEach((item) => collect(item, output));
                  return output;
                }
                if (typeof value === "object") {
                  Object.entries(value).forEach(([key, item]) => {
                    if (/email|name|account|user/i.test(key)) output.push(`${key}: ${String(item)}`);
                    collect(item, output);
                  });
                }
                return output;
              };
              for (const endpoint of endpoints) {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 2500);
                try {
                  const response = await fetch(endpoint, { credentials: "include", signal: controller.signal });
                  clearTimeout(timer);
                  if (!response.ok) continue;
                  const data = await response.json();
                  const values = collect(data, []);
                  if (values.length) return values.join("\\n").slice(0, 12000);
                } catch {
                  clearTimeout(timer);
                }
              }
              return "";
            }
            """
        )

    async def _detect_account_from_profile_menu(self) -> str:
        if self.page is None:
            return ""
        selectors = [
            'button[data-testid="profile-button"]',
            'button[aria-label*="Profile"]',
            'button[aria-label*="profile"]',
            'button[aria-label*="account"]',
            'button[aria-label*="Account"]',
            'button:has(img[alt])',
        ]
        for selector in selectors:
            try:
                locator = self.page.locator(selector).first
                if await locator.count() == 0 or not await locator.is_visible(timeout=700):
                    continue
                await locator.click(timeout=PLAYWRIGHT_ACTION_TIMEOUT_MS)
                await self.page.wait_for_timeout(700)
                text = await self.page.evaluate(
                    """
                    () => {
                      const roots = [
                        ...document.querySelectorAll('[role="menu"], [role="dialog"], [data-radix-popper-content-wrapper]'),
                        document.body,
                      ];
                      return roots.map((root) => root.innerText || root.textContent || "").join("\\n").slice(0, 12000);
                    }
                    """
                )
                await self.page.keyboard.press("Escape")
                return text
            except Exception:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
                continue
        return ""

    @classmethod
    def _clean_account_label(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        email_match = EMAIL_PATTERN.search(text)
        if email_match:
            return email_match.group(0)[:200]
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        generic_markers = (
            "profile",
            "account",
            "menu",
            "open",
            "button",
            "settings",
            "\u5e33\u6236",
            "\u5e33\u865f",
        )
        for line in lines:
            lowered = line.lower()
            if 2 <= len(line) <= 200 and not any(marker in lowered for marker in generic_markers):
                return line
        return ""

    def _set_dispatch_error(self, message: str) -> None:
        self.last_dispatch_error = message
        print(f"[ChatGPT] dispatch detail: {message}")

    async def _visible_first(self, selectors: list[str]):
        if self.page is None:
            return None, ""
        for selector in selectors:
            locator = self.page.locator(selector)
            count = await locator.count()
            for index in range(min(count, 5)):
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
        await self.page.wait_for_timeout(300)

    async def _click_send(self) -> tuple[bool, str]:
        if self.page is None:
            return False, ""
        for _ in range(10):
            send_button, selector = await self._visible_first(CHATGPT_SELECTORS["send"])
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
            
        # 發送內容前先等待 3 秒
        await asyncio.sleep(3)

        input_box, input_selector = await self._visible_first(CHATGPT_SELECTORS["input"])
        if input_box is None:
            self._set_dispatch_error(f"prompt input not found; url={self.page.url}")
            return False

        await self._type_prompt(input_box, prompt_text)

        sent, send_selector = await self._click_send()
        if sent:
            print(f"[ChatGPT] prompt sent input={input_selector} send={send_selector} url={self.page.url}")
            return True

        try:
            await self.page.keyboard.press("Enter")
            await self.page.wait_for_timeout(500)
            self._set_dispatch_error(f"send button not found; used Enter fallback; input={input_selector}; url={self.page.url}")
            return True
        except Exception as exc:
            self._set_dispatch_error(f"send button not found and Enter fallback failed: {exc}; url={self.page.url}")
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

        for selector in CHATGPT_SELECTORS["response"]:
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
            print(f"[ChatGPT] capture timeout ({timeout_seconds}s)")
            return ""
        except PlaywrightTimeoutError as e:
            print(f"[ChatGPT] capture playwright timeout: {e}")
            return ""
        except Exception as e:
            print(f"[ChatGPT] capture error: {e}")
            return ""

    async def recover(self) -> bool:
        try:
            self.page = await self.session_manager.ensure_chatgpt_page()
            return True
        except Exception as e:
            print(f"[ChatGPT] recover error: {e}")
            return False
