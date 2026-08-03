from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from ..domain.task_protocol import build_memory_candidate
from .browser_automation import BrowserAutomationSession


class AiCollaborationProviderSession:
    """Automated provider execution in visible Google Chrome tabs."""

    BROWSER_PRIMARY_PROVIDERS = frozenset(
        {"chatgpt", "claude", "gemini", "grok", "deepseek", "perplexity"}
    )

    def __init__(self, project_root: Path) -> None:
        project_root = Path(project_root).resolve()
        if project_root.name != "ai-collaboration":
            project_root = project_root / "ai-collaboration"
        self.project_root = project_root
        self._profile_root = (
            project_root
            / "runtime"
            / "browser-profiles"
            / "ai-collaboration"
            / "shared"
        )
        chrome = self._resolve_google_chrome()
        self.browser = (
            BrowserAutomationSession(
                self._profile_root,
                chrome,
            )
            if chrome is not None
            else None
        )

    async def send_task(
        self, agent: dict[str, Any], task: dict[str, Any]
    ) -> dict[str, Any]:
        provider = str(agent.get("provider") or "").strip().casefold()
        if provider not in self.BROWSER_PRIMARY_PROVIDERS:
            return {
                "status": "failed",
                "content": "",
                "error": "UNSUPPORTED_BROWSER_PROVIDER",
                "error_code": "UNSUPPORTED_BROWSER_PROVIDER",
                "provider": provider,
                "transport": "google-chrome-shared-foreground-tabs",
                "uses_api_key": False,
                "memory_candidates": [],
            }
        browser = self._available_browser()
        if browser is None:
            return {
                "status": "failed",
                "content": "",
                "error": "GOOGLE_CHROME_NOT_INSTALLED",
                "error_code": "GOOGLE_CHROME_NOT_INSTALLED",
                "provider": provider,
                "transport": "google-chrome-playwright-foreground",
                "uses_api_key": False,
                "memory_candidates": [],
                "fallback": {
                    "used": False,
                    "browser_only": True,
                    "cross_provider_substitution": False,
                },
            }
        result = await browser.send_prompt(agent, str(task.get("content") or ""))
        if (
            result.get("status") == "completed"
            and task.get("memory_policy", {}).get("writeback") == "candidate-only"
        ):
            result["memory_candidates"] = [
                build_memory_candidate(
                    task,
                    source_agent_id=str(agent.get("agent_id") or provider),
                    content=str(result.get("content") or ""),
                )
            ]
        return result

    async def send_terminal_fallback(
        self,
        agent: dict[str, Any],
        task: dict[str, Any],
        reason: str,
    ) -> dict[str, Any]:
        provider = str(agent.get("provider") or "").strip().casefold()
        return {
            "status": "failed",
            "content": "",
            "error": reason,
            "error_code": reason,
            "provider": provider,
            "transport": "google-chrome-shared-foreground-tabs",
            "uses_api_key": False,
            "memory_candidates": [],
            "fallback": {
                "used": False,
                "browser_only": True,
                "cross_provider_substitution": False,
            },
        }

    async def open_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        url = str(agent.get("home_url") or "").strip()
        if not url.startswith("https://"):
            return {"ok": False, "message": "INVALID_EXTERNAL_URL"}
        browser = self._available_browser()
        if browser is None:
            return {
                "ok": False,
                "message": "GOOGLE_CHROME_NOT_INSTALLED",
                "error_code": "GOOGLE_CHROME_NOT_INSTALLED",
            }
        return await browser.open_agent(agent)

    def _available_browser(self) -> BrowserAutomationSession | None:
        """Re-detect Chrome so a running tool need not be restarted after install."""

        if self.browser is not None:
            return self.browser
        chrome = self._resolve_google_chrome()
        if chrome is None:
            return None
        self.browser = BrowserAutomationSession(self._profile_root, chrome)
        return self.browser

    async def authorize_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        requested_provider = str(agent.get("provider") or "").strip().casefold()
        if requested_provider not in self.BROWSER_PRIMARY_PROVIDERS:
            return {"ok": False, "message": "UNSUPPORTED_PROVIDER_AUTHORIZATION"}
        result = await self.open_agent(agent)
        return {
            **result,
            "authorization_provider": f"{requested_provider}-browser-session",
            "mode": "google-chrome-playwright-foreground",
            "uses_api_key": False,
            "interactive_browser": True,
            "automatic_inference_fallback": None,
        }

    @staticmethod
    def _resolve_google_chrome() -> Path | None:
        discovered = shutil.which("chrome") or shutil.which("chrome.exe")
        candidates = [Path(discovered)] if discovered else []
        for environment_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = str(os.environ.get(environment_name) or "").strip()
            if base:
                candidates.append(
                    Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"
                )
        return next((path.resolve() for path in candidates if path.is_file()), None)

    def provider_status(self) -> list[dict[str, Any]]:
        browser_available = self._resolve_google_chrome() is not None
        return [
            {
                "provider": provider,
                "mode": "google-chrome-playwright-foreground",
                "installed": browser_available,
                "uses_api_key": False,
                "background_resident": False,
                "browser_only": True,
                "automation": True,
                "foreground": True,
                "shared_browser_context": True,
                "terminal_fallback": False,
            }
            for provider in sorted(self.BROWSER_PRIMARY_PROVIDERS)
        ]

    def browser_status(self) -> dict[str, Any]:
        chrome = self._resolve_google_chrome()
        return {
            "product": "google-chrome",
            "available": chrome is not None,
            "ready_without_restart": chrome is not None,
            "mode": "playwright-shared-foreground-tabs",
            "automation": True,
            "foreground": True,
            "shared_browser_context": True,
        }

    async def close_background_context(self) -> None:
        return None

    async def shutdown(self) -> None:
        if self.browser is not None:
            await self.browser.shutdown()
