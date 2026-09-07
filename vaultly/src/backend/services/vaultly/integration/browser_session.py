from __future__ import annotations

import asyncio
from typing import Any

from shared_layer.embedded_browser_client import (
    EmbeddedBrowserClient,
    InProcessEmbeddedBrowser,
)


class _EmbeddedPage:
    """Compatibility shim that mimics the Playwright Page interface.

    Allows existing vaultly service code to use `page.url`,
    `page.bring_to_front()`, etc. without changes — but all operations
    are routed to the embedded Electron BrowserView, not an external
    browser.
    """

    def __init__(
        self,
        session_id: str,
        client: InProcessEmbeddedBrowser,
    ) -> None:
        self._session_id = session_id
        self._client = client

    @property
    def url(self) -> str:
        url = self._client.get_url(self._session_id)
        return url or ""

    async def bring_to_front(self) -> None:
        self._client.show(self._session_id)

    async def goto(self, url: str, **_kwargs: Any) -> None:
        self._client.navigate(self._session_id, url)

    async def close(self) -> None:
        self._client.close(self._session_id)

    @property
    def is_closed(self) -> bool:
        return self._client.get_url(self._session_id) is None

    async def evaluate(self, script: str) -> Any:
        result = self._client.execute_script(self._session_id, script)
        return result.get("result") if result.get("ok") else None


class BrowserSessionManager:
    """Embedded browser session manager — replaces Playwright + Edge.

    All browser operations happen inside the Electron main window via
    BrowserView.  No external Edge/Chrome process is launched.
    """

    def __init__(
        self,
        profile_name: str = "vaultly",
        headless: bool = True,
        profile_root: Any = None,
    ) -> None:
        # profile_name, headless, profile_root accepted for backward
        # compatibility but no longer used — the embedded browser manages
        # its own state inside Electron.
        self._profile_name = profile_name
        self._client = InProcessEmbeddedBrowser()
        self._ipc = EmbeddedBrowserClient()
        self.external_pages: dict[str, _EmbeddedPage] = {}
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._external_lock = asyncio.Lock()

    @property
    def is_initialized(self) -> bool:
        return self._initialized

    async def ensure_initialized(self) -> None:
        if self.is_initialized:
            return
        async with self._init_lock:
            if self.is_initialized:
                return
            await self.initialize()

    async def initialize(self) -> None:
        self._initialized = True

    async def ensure_external_page(
        self,
        key: str,
        target_url: str = "",
        match_hosts: tuple[str, ...] = (),
    ) -> _EmbeddedPage:
        normalized_key = key.strip()
        if not normalized_key:
            raise ValueError("External page key is required")

        await self.ensure_initialized()
        async with self._external_lock:
            page = self.external_pages.get(normalized_key)
            if page is None or page.is_closed:
                result = self._client.create_session(
                    owner_module=self._profile_name,
                    url=target_url or "about:blank",
                )
                if result.get("ok"):
                    page = _EmbeddedPage(str(result["id"]), self._client)
                else:
                    raise RuntimeError(
                        f"EMBEDDED_BROWSER_SESSION_FAILED: {result.get('message')}"
                    )

            self.external_pages[normalized_key] = page
            if target_url and (page.url == "about:blank" or page.url != target_url):
                await page.goto(target_url)
            return page

    async def shutdown(self) -> None:
        for page in self.external_pages.values():
            try:
                await page.close()
            except Exception:
                pass
        self.external_pages.clear()
        self._initialized = False
