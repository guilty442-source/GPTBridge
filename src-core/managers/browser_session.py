from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Awaitable, Callable, List, Optional, Union

from playwright.async_api import BrowserContext, Page, async_playwright

from settings.config import load_config, update_config_url


class BrowserSessionManager:
    PROVIDERS = {
        "chatgpt": {
            "main_key": "chatgpt_main_url",
            "developer_key": "chatgpt_developer_url",
            "audit_key": "chatgpt_developer_url",
            "default_main": "https://chatgpt.com/c/6a106776-d43c-83a4-a417-11f84b6b1e8d",
            "default_developer": "https://chatgpt.com/c/6a0b05ab-e2ec-83a5-8140-ef41cff289a1",
            "default_audit": "https://chatgpt.com/c/6a0b05ab-e2ec-83a5-8140-ef41cff289a1",
            "default_channel": "msedge",
            "match": ("chatgpt.com",),
        },
        "gemini": {
            "main_key": "gemini_main_url",
            "developer_key": "gemini_developer_url",
            "audit_key": "gemini_developer_url",
            "default_main": "https://gemini.google.com/app/620770e7f10e50bf",
            "default_developer": "https://gemini.google.com/app/b209461647349329",
            "default_audit": "https://gemini.google.com/app/b209461647349329",
            "default_channel": "msedge",
            "match": ("gemini.google.com",),
        },
        "claude": {
            "main_key": "claude_main_url",
            "developer_key": "claude_developer_url",
            "audit_key": "claude_developer_url",
            "default_main": "https://claude.ai/new",
            "default_developer": "https://claude.ai/new",
            "default_audit": "https://claude.ai/new",
            "default_channel": "msedge",
            "match": ("claude.ai",),
        },
        "perplexity": {
            "main_key": "perplexity_main_url",
            "developer_key": "perplexity_developer_url",
            "audit_key": "perplexity_developer_url",
            "default_main": "https://www.perplexity.ai/",
            "default_developer": "https://www.perplexity.ai/",
            "default_audit": "https://www.perplexity.ai/",
            "default_channel": "msedge",
            "match": ("perplexity.ai",),
        },
        "deepseek": {
            "main_key": "deepseek_main_url",
            "developer_key": "deepseek_developer_url",
            "audit_key": "deepseek_developer_url",
            "default_main": "https://chat.deepseek.com/",
            "default_developer": "https://chat.deepseek.com/",
            "default_audit": "https://chat.deepseek.com/",
            "default_channel": "msedge",
            "match": ("deepseek.com",),
        },
    }

    def __init__(
        self,
        profile_name: str = "main",
        headless: bool = False,
        profile_root: Path | None = None,
    ) -> None:
        project_root = Path(
            os.environ.get("GPTBRIDGE_PROJECT_ROOT")
            or Path(__file__).resolve().parent.parent
        ).resolve()
        resolved_profile_root = (
            profile_root.resolve()
            if profile_root is not None
            else project_root / "edge-profile"
        )
        self.profile_dir = resolved_profile_root / profile_name
        self.shared_profile_dir = self.profile_dir / "shared"
        self.playwright = None
        self.context: Optional[BrowserContext] = None
        self.contexts: dict[str, BrowserContext] = {}
        self.external_pages: dict[str, Page] = {}
        self.chatgpt_page: Optional[Page] = None
        self.gemini_page: Optional[Page] = None
        self.claude_page: Optional[Page] = None
        self.perplexity_page: Optional[Page] = None
        self.deepseek_page: Optional[Page] = None
        self.headless = headless
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._provider_locks = {
            provider: asyncio.Lock() for provider in self.PROVIDERS
        }
        self._external_lock = asyncio.Lock()
        self.retry_state: dict[str, dict[str, int]] = {
            provider: {} for provider in self.PROVIDERS
        }
        self.health_state: dict[str, str] = {
            provider: "UNOPENED" for provider in self.PROVIDERS
        }
        self.page_targets: dict[str, str] = {
            provider: "" for provider in self.PROVIDERS
        }
        self._on_initialize_callbacks: List[Callable[[], Union[None, Awaitable[None]]]] = []

    @property
    def is_initialized(self) -> bool:
        return self._initialized and self.playwright is not None

    def _context_is_usable(self) -> bool:
        if self.context is None:
            return False
        try:
            _ = self.context.pages
            return True
        except Exception:
            return False

    def _mark_closed(self) -> None:
        self._initialized = False
        self.context = None
        self.contexts = {}
        self.external_pages = {}
        self.chatgpt_page = None
        self.gemini_page = None
        self.claude_page = None
        self.perplexity_page = None
        self.deepseek_page = None
        for provider in self.health_state:
            self.health_state[provider] = "CLOSED"
            self.page_targets[provider] = ""

    def _mark_provider_closed(self, provider: str) -> None:
        self._set_tracked_page(provider, None)
        self.contexts.pop(provider, None)
        self.health_state[provider] = "CLOSED"
        self.page_targets[provider] = ""

    async def _reset_closed_session(self) -> None:
        if self.playwright:
            try:
                await self._close_all_contexts()
                await self.playwright.stop()
            except Exception:
                pass
            finally:
                self.playwright = None
        self._mark_closed()

    def on_initialize(self, callback: Callable[[], Union[None, Awaitable[None]]]) -> None:
        self._on_initialize_callbacks.append(callback)

    async def initialize(self, headless: Optional[bool] = None) -> None:
        browser_behavior = load_config().get("browser_behavior", {})
        if headless is not None:
            self.headless = headless
        else:
            self.headless = bool(browser_behavior.get("headless", self.headless))

        self.shared_profile_dir.mkdir(parents=True, exist_ok=True)

        if self.playwright:
            try:
                await self._close_all_contexts()
                await self.playwright.stop()
            except Exception:
                pass

        self.context = None
        self.contexts = {}
        self.external_pages = {}
        self.chatgpt_page = None
        self.gemini_page = None
        self.claude_page = None
        self.perplexity_page = None
        self.deepseek_page = None
        self.page_targets = {provider: "" for provider in self.PROVIDERS}
        self._initialized = False
        self.playwright = await async_playwright().start()

        # Start Playwright only. Shared Edge context launches on first provider demand.
        self._initialized = True

        for callback in self._on_initialize_callbacks:
            if asyncio.iscoroutinefunction(callback):
                await callback()
            elif callable(callback):
                result = callback()
                if asyncio.iscoroutine(result):
                    await result

    async def ensure_initialized(self) -> None:
        if self.is_initialized:
            return
        async with self._init_lock:
            if self.is_initialized:
                return
            await self._reset_closed_session()
            await self.initialize()

    async def ensure_pages(self) -> None:
        await asyncio.gather(
            self.ensure_chatgpt_page(),
            self.ensure_gemini_page(),
            self.ensure_claude_page(),
            self.ensure_perplexity_page(),
            self.ensure_deepseek_page(),
        )

    async def ensure_chatgpt_page(self, target: str = "main") -> Page:
        return await self.ensure_provider_page("chatgpt", target)

    async def ensure_gemini_page(self, target: str = "main") -> Page:
        return await self.ensure_provider_page("gemini", target)

    async def ensure_claude_page(self, target: str = "main") -> Page:
        return await self.ensure_provider_page("claude", target)

    async def ensure_perplexity_page(self, target: str = "main") -> Page:
        return await self.ensure_provider_page("perplexity", target)

    async def ensure_deepseek_page(self, target: str = "main") -> Page:
        return await self.ensure_provider_page("deepseek", target)

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
            context = await self._ensure_provider_context("chatgpt")
            page = self.external_pages.get(normalized_key)
            if not self._page_is_open(page):
                page = self._find_external_page(match_hosts)
            if page is None:
                page = await context.new_page()

            self.external_pages[normalized_key] = page
            if target_url and (self._is_blank_page(page) or self._needs_navigation(page, target_url)):
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            return page

    async def ensure_provider_page(self, provider: str, target: str = "main") -> Page:
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                return await self._ensure_provider_page_once(provider, target)
            except Exception as exc:
                last_error = exc
                if not self._is_closed_browser_error(exc):
                    await asyncio.sleep(3.0)
                    continue
                self._mark_closed()
                await asyncio.sleep(3.0)
        raise RuntimeError(f"{provider} browser page is unavailable: {last_error}") from last_error

    async def _ensure_provider_page_once(self, provider: str, target: str = "main") -> Page:
        await self.ensure_initialized()
        context = await self._ensure_provider_context(provider)
        target_url = self._provider_url(provider, target)

        page = self._tracked_page(provider)
        if self._page_is_open(page):
            if self._needs_navigation(page, target_url):
                try:
                    await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    if self._is_closed_browser_error(e):
                        raise
                    print(f"{provider} navigation warning: {e}")
            self.page_targets[provider] = target
            return page

        page = self._find_reusable_page(provider)
        should_navigate = page is None or self._is_blank_page(page)
        if page is None:
            page = await context.new_page()

        self._set_tracked_page(provider, page)
        if should_navigate or self._needs_navigation(page, target_url):
            try:
                await page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                if self._is_closed_browser_error(e):
                    raise
                print(f"{provider} navigation warning: {e}")

        self.page_targets[provider] = target
        return page

    async def focus_chatgpt(self, target: str = "main") -> None:
        page = await self.ensure_chatgpt_page(target)
        await page.bring_to_front()

    async def focus_gemini(self, target: str = "main") -> None:
        page = await self.ensure_gemini_page(target)
        await page.bring_to_front()

    async def focus_claude(self, target: str = "main") -> None:
        page = await self.ensure_claude_page(target)
        await page.bring_to_front()

    async def focus_perplexity(self, target: str = "main") -> None:
        page = await self.ensure_perplexity_page(target)
        await page.bring_to_front()

    async def focus_deepseek(self, target: str = "main") -> None:
        page = await self.ensure_deepseek_page(target)
        await page.bring_to_front()

    async def set_provider_url(self, provider: str, target: str, url: str) -> None:
        self._validate_provider(provider)
        config_key = f"{provider}_{target}_url"
        update_config_url(config_key, url)
        page = await self.ensure_provider_page(provider, target)
        await page.goto(url, wait_until="domcontentloaded")

    async def restart_provider(self, provider: str, preserve_session: bool = True) -> None:
        self._validate_provider(provider)
        page = self._tracked_page(provider)
        if self._page_is_open(page):
            try:
                await page.close()
            except Exception:
                pass
        self._set_tracked_page(provider, None)
        self.health_state[provider] = "RESTARTING"
        if not preserve_session:
            self.retry_state[provider] = {}
        await self._ensure_provider_context(provider)

    async def shutdown(self) -> None:
        await self._close_all_contexts()

        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass

        self._mark_closed()
        self.playwright = None

    async def _close_all_contexts(self) -> None:
        context = self.context
        self.context = None
        self.contexts = {}
        self.external_pages = {}
        for provider in self.PROVIDERS:
            self._set_tracked_page(provider, None)
        if context is None:
            return
        try:
            await context.close()
        except Exception:
            pass

    def _provider_url(self, provider: str, target: str = "main") -> str:
        self._validate_provider(provider)
        if target not in {"main", "developer", "audit"}:
            raise ValueError(f"Unknown provider target: {target}")
        info = self.PROVIDERS[provider]
        config = load_config() or {}
        key = info[f"{target}_key"]
        return config.get(key) or info[f"default_{target}"]

    def _validate_provider(self, provider: str) -> None:
        if provider not in self.PROVIDERS:
            raise ValueError(f"Unknown provider: {provider}")

    def _tracked_page(self, provider: str) -> Optional[Page]:
        self._validate_provider(provider)
        return getattr(self, f"{provider}_page", None)

    def _set_tracked_page(self, provider: str, page: Optional[Page]) -> None:
        self._validate_provider(provider)
        setattr(self, f"{provider}_page", page)

    def _page_is_open(self, page: Optional[Page]) -> bool:
        try:
            return page is not None and not page.is_closed()
        except Exception:
            return False

    def _matches_provider(self, page: Page, provider: str) -> bool:
        try:
            url = page.url.lower()
        except Exception:
            return False
        return any(marker in url for marker in self.PROVIDERS[provider]["match"])

    def _is_blank_page(self, page: Page) -> bool:
        try:
            url = page.url.lower()
        except Exception:
            return False
        return url in {"about:blank", "chrome://new-tab-page/", "edge://newtab/"}

    def _needs_navigation(self, page: Page, target_url: str) -> bool:
        try:
            current_url = page.url.rstrip("/")
        except Exception:
            return True
        normalized_target = target_url.rstrip("/")
        return not (current_url == normalized_target or current_url.startswith(f"{normalized_target}?"))

    def _find_reusable_page(self, provider: str) -> Optional[Page]:
        context = self.context
        if context is None:
            return None

        for page in context.pages:
            if self._page_is_open(page) and self._matches_provider(page, provider):
                return page

        tracked_pages = {
            id(page)
            for page in (
                getattr(self, f"{provider_name}_page", None)
                for provider_name in self.PROVIDERS
            )
            if page is not None
        }
        tracked_pages.update(id(page) for page in self.external_pages.values() if page is not None)
        for page in context.pages:
            if id(page) in tracked_pages:
                continue
            if self._page_is_open(page) and self._is_blank_page(page):
                return page

        return None

    def _find_external_page(self, match_hosts: tuple[str, ...]) -> Optional[Page]:
        context = self.context
        if context is None:
            return None

        normalized_hosts = tuple(host.casefold() for host in match_hosts if host)
        for page in context.pages:
            if not self._page_is_open(page):
                continue
            try:
                url = page.url.casefold()
            except Exception:
                continue
            if normalized_hosts and any(host in url for host in normalized_hosts):
                return page

        tracked_pages = {
            id(page)
            for page in (
                *self.external_pages.values(),
                *(getattr(self, f"{provider_name}_page", None) for provider_name in self.PROVIDERS),
            )
            if page is not None
        }
        for page in context.pages:
            if id(page) not in tracked_pages and self._page_is_open(page) and self._is_blank_page(page):
                return page
        return None

    async def _ensure_provider_context(self, provider: str) -> BrowserContext:
        self._validate_provider(provider)
        await self.ensure_initialized()
        async with self._provider_locks[provider]:
            if self._context_is_usable() and self.context is not None:
                self.contexts[provider] = self.context
                self.health_state[provider] = "READY"
                return self.context

            if self.context is not None:
                try:
                    await self.context.close()
                except Exception:
                    pass
                self.context = None
                self.contexts = {}

            if self.playwright is None:
                raise RuntimeError("Playwright is not available")

            self.shared_profile_dir.mkdir(parents=True, exist_ok=True)
            browser_behavior = (load_config() or {}).get("browser_behavior", {})
            preferred_channels: list[str] = ["msedge"]
            browser_channel = preferred_channels[0]
            print(f"[BrowserSession] Launching shared session using browser: {browser_channel}")

            launch_args = ["--disable-blink-features=AutomationControlled"]
            launch_args.append("--start-minimized" if browser_behavior.get("background", True) else "--start-maximized")
            try:
                context = await self.playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.shared_profile_dir),
                    headless=self.headless,
                    channel=browser_channel,
                    locale="zh-TW",
                    viewport=None,
                    args=launch_args,
                )
                context.on("close", lambda _context=None: self._mark_closed())
            except Exception as e:
                self._mark_closed()
                if "profile" in str(e).lower() or "used by another" in str(e).lower():
                    raise RuntimeError(
                        "BROWSER_LOCKED: shared Edge profile is already in use. Close the existing Edge window first."
                    ) from e
                raise

            self.context = context
            self.contexts = {provider_name: context for provider_name in self.PROVIDERS}
            self.health_state[provider] = "READY"
            return context

    @staticmethod
    def _is_closed_browser_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return (
            "target page" in text
            or "context or browser has been closed" in text
            or "browser has been closed" in text
            or "context has been closed" in text
        )

    def _provider_context_is_usable(self, provider: str) -> bool:
        context = self.contexts.get(provider)
        if context is None:
            return False
        try:
            _ = context.pages
            return True
        except Exception:
            return False
