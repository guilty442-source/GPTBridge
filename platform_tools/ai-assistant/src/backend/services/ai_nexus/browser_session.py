from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import BrowserContext, Page, async_playwright


VERIFICATION_MARKERS = (
    "captcha",
    "cloudflare",
    "verify you are human",
    "checking your browser",
    "human verification",
    "are you a robot",
    "請驗證",
    "驗證你是人類",
)


SUBMIT_SCRIPT = """
async ({ prompt }) => {
  const isVisible = (element) => {
    const style = window.getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
  };
  const selectors = [
    'textarea',
    '[contenteditable="true"]',
    'div[role="textbox"]',
    'p[data-placeholder]',
    '[aria-label*="prompt" i]',
    '[aria-label*="message" i]',
    '[aria-label*="訊息"]',
    '[aria-label*="提示"]'
  ];
  const candidates = selectors
    .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
    .filter(isVisible);
  const target = candidates[candidates.length - 1];
  if (!target) return { ok: false, message: '找不到可輸入的文字框，請手動完成登入或切到新對話。' };
  target.focus();
  if (target.tagName === 'TEXTAREA' || target.tagName === 'INPUT') {
    target.value = prompt;
    target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: prompt }));
  } else {
    target.textContent = prompt;
    target.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: prompt }));
  }
  await new Promise((resolve) => setTimeout(resolve, 180));
  const buttonSelectors = [
    'button[type="submit"]',
    'button[aria-label*="Send" i]',
    'button[aria-label*="Submit" i]',
    'button[aria-label*="傳送"]',
    'button[aria-label*="送出"]',
    'button[data-testid*="send" i]',
    'button:has(svg)'
  ];
  const buttons = buttonSelectors
    .flatMap((selector) => Array.from(document.querySelectorAll(selector)))
    .filter(isVisible);
  const enabled = buttons.find((button) => !button.disabled && button.getAttribute('aria-disabled') !== 'true');
  if (enabled) {
    enabled.click();
    return { ok: true, method: 'button' };
  }
  target.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
  return { ok: true, method: 'enter' };
}
"""


CAPTURE_SCRIPT = """
() => {
  const selectors = [
    '[data-message-author-role="assistant"]',
    '[data-testid*="conversation-turn"]',
    '.markdown',
    'article',
    '[class*="message"]',
    '[class*="response"]'
  ];
  const seen = new Set();
  const texts = [];
  for (const selector of selectors) {
    for (const element of document.querySelectorAll(selector)) {
      const text = (element.innerText || element.textContent || '').trim();
      if (text.length < 12 || seen.has(text)) continue;
      seen.add(text);
      texts.push(text);
    }
  }
  if (texts.length > 0) return texts[texts.length - 1];
  return (document.body.innerText || '').trim().slice(-4000);
}
"""


class AiNexusBrowserSession:
    def __init__(
        self,
        profile_name: str = "ai-assistant",
        profile_root: Path | None = None,
    ) -> None:
        root = profile_root.resolve() if profile_root is not None else Path.cwd() / "runtime" / "browser-profiles"
        self.profile_dir = root / profile_name
        self.shared_profile_dir = self.profile_dir / "shared"
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.pages: dict[str, Page] = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._page_lock = asyncio.Lock()

    @property
    def is_initialized(self) -> bool:
        return self._initialized and self.playwright is not None

    async def ensure_initialized(self) -> None:
        if self.is_initialized:
            return
        async with self._init_lock:
            if self.is_initialized:
                return
            self.shared_profile_dir.mkdir(parents=True, exist_ok=True)
            self.playwright = await async_playwright().start()
            self._initialized = True

    async def ensure_agent_page(self, agent: dict[str, Any]) -> Page:
        agent_id = str(agent.get("agent_id", "")).strip()
        if not agent_id:
            raise ValueError("agent_id is required")
        await self.ensure_initialized()
        async with self._page_lock:
            context = await self._ensure_context()
            page = self.pages.get(agent_id)
            if not self._page_is_open(page):
                page = await context.new_page()
                self.pages[agent_id] = page
            target_url = str(agent.get("home_url", "")).strip()
            if target_url and self._needs_navigation(page, target_url):
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            return page

    async def open_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        page = await self.ensure_agent_page(agent)
        await page.bring_to_front()
        return {"ok": True, "url": page.url}

    async def send_prompt(self, agent: dict[str, Any], prompt: str) -> dict[str, str]:
        page = await self.ensure_agent_page(agent)
        await page.bring_to_front()
        if await self._verification_required(page):
            return {
                "status": "waiting_verification",
                "content": "",
                "error": "請手動完成驗證後再重試。",
            }
        submit = await page.evaluate(SUBMIT_SCRIPT, {"prompt": prompt})
        if not isinstance(submit, dict) or submit.get("ok") is not True:
            return {
                "status": "failed",
                "content": "",
                "error": str((submit or {}).get("message", "送出失敗")),
            }
        await page.wait_for_timeout(4500)
        text = str(await page.evaluate(CAPTURE_SCRIPT) or "").strip()
        if not text:
            return {"status": "failed", "content": "", "error": "未擷取到 AI 回覆"}
        return {"status": "completed", "content": text, "error": ""}

    async def shutdown(self) -> None:
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
        self.context = None
        self.pages = {}
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self.playwright = None
        self._initialized = False

    async def _ensure_context(self) -> BrowserContext:
        if self.context is not None and self._context_is_usable():
            return self.context
        if self.playwright is None:
            raise RuntimeError("Playwright is not available")
        self.shared_profile_dir.mkdir(parents=True, exist_ok=True)
        self.context = await self.playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.shared_profile_dir),
            channel="msedge",
            headless=False,
            locale="zh-TW",
            viewport=None,
        )
        self.context.on("close", lambda _context=None: self._mark_closed())
        return self.context

    def _mark_closed(self) -> None:
        self.context = None
        self.pages = {}
        self._initialized = self.playwright is not None

    def _context_is_usable(self) -> bool:
        try:
            return self.context is not None and self.context.pages is not None
        except Exception:
            return False

    async def _verification_required(self, page: Page) -> bool:
        try:
            text = str(await page.evaluate("() => document.body.innerText || ''"))
        except Exception:
            return False
        normalized = re.sub(r"\s+", " ", text).casefold()
        return any(marker in normalized for marker in VERIFICATION_MARKERS)

    @staticmethod
    def _page_is_open(page: Optional[Page]) -> bool:
        try:
            return page is not None and not page.is_closed()
        except Exception:
            return False

    @staticmethod
    def _needs_navigation(page: Page, target_url: str) -> bool:
        try:
            current_url = page.url.rstrip("/")
        except Exception:
            return True
        normalized_target = target_url.rstrip("/")
        return not (
            current_url == normalized_target
            or current_url.startswith(f"{normalized_target}/")
            or current_url.startswith(f"{normalized_target}?")
        )
