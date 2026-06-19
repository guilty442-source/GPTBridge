from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from playwright.async_api import BrowserContext, Page, async_playwright


class BrowserSessionManager:
    def __init__(
        self,
        profile_name: str = "vaultly",
        headless: bool = True,
        profile_root: Path | None = None,
    ) -> None:
        root = profile_root.resolve() if profile_root is not None else Path.cwd() / "runtime" / "browser-profiles"
        self.profile_dir = root / profile_name
        self.shared_profile_dir = self.profile_dir / "shared"
        self.headless = headless
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.external_pages: dict[str, Page] = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._external_lock = asyncio.Lock()

    @property
    def is_initialized(self) -> bool:
        return self._initialized and self.playwright is not None

    async def ensure_initialized(self) -> None:
        if self.is_initialized:
            return
        async with self._init_lock:
            if self.is_initialized:
                return
            await self.initialize()

    async def initialize(self) -> None:
        self.shared_profile_dir.mkdir(parents=True, exist_ok=True)
        if self.playwright is not None:
            await self.shutdown()
        self.playwright = await async_playwright().start()
        self._initialized = True

    async def ensure_external_page(
        self,
        key: str,
        target_url: str = "",
        match_hosts: tuple[str, ...] = (),
    ) -> Page:
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("External page key is required")

        await self.ensure_initialized()
        async with self._external_lock:
            context = await self._ensure_context()
            page = self.external_pages.get(normalized_key)
            if not self._page_is_open(page):
                page = self._find_external_page(match_hosts)
            if page is None:
                page = await context.new_page()

            self.external_pages[normalized_key] = page
            if target_url and (self._is_blank_page(page) or self._needs_navigation(page, target_url)):
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            return page

    async def shutdown(self) -> None:
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
        self.context = None
        self.external_pages = {}
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
        try:
            self.context = await self.playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.shared_profile_dir),
                headless=self.headless,
                channel="msedge",
                locale="zh-TW",
                viewport=None,
                args=["--disable-blink-features=AutomationControlled", "--start-minimized"],
            )
        except Exception as exc:
            if "profile" in str(exc).lower() or "used by another" in str(exc).lower():
                raise RuntimeError(
                    "BROWSER_LOCKED: Vaultly Edge profile is already in use. Close the existing Edge window first."
                ) from exc
            raise
        self.context.on("close", lambda _context=None: self._mark_closed())
        return self.context

    def _mark_closed(self) -> None:
        self.context = None
        self.external_pages = {}
        self._initialized = self.playwright is not None

    def _context_is_usable(self) -> bool:
        try:
            return self.context is not None and self.context.pages is not None
        except Exception:
            return False

    def _find_external_page(self, match_hosts: tuple[str, ...]) -> Optional[Page]:
        if self.context is None:
            return None
        normalized_hosts = tuple(host.casefold() for host in match_hosts if host)
        for page in self.context.pages:
            if not self._page_is_open(page):
                continue
            try:
                url = page.url.casefold()
            except Exception:
                continue
            if normalized_hosts and any(host in url for host in normalized_hosts):
                return page
        for page in self.context.pages:
            if self._page_is_open(page) and self._is_blank_page(page):
                return page
        return None

    @staticmethod
    def _page_is_open(page: Optional[Page]) -> bool:
        try:
            return page is not None and not page.is_closed()
        except Exception:
            return False

    @staticmethod
    def _is_blank_page(page: Page) -> bool:
        try:
            url = page.url.lower()
        except Exception:
            return False
        return url in {"about:blank", "chrome://new-tab-page/", "edge://newtab/"}

    @staticmethod
    def _needs_navigation(page: Page, target_url: str) -> bool:
        try:
            current_url = page.url.rstrip("/")
        except Exception:
            return True
        normalized_target = target_url.rstrip("/")
        return not (
            current_url == normalized_target
            or current_url.startswith(f"{normalized_target}?")
        )
