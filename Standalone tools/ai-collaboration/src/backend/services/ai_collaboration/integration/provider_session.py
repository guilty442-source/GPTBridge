from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.task_protocol import build_memory_candidate
from .browser_automation import BrowserAutomationSession


class AiCollaborationProviderSession:
    """Automated provider execution using the embedded Electron BrowserView.

    No external Chrome/Edge is launched.  All browser operations happen
    inside the main Electron window via the embedded browser module.
    """

    BROWSER_PRIMARY_PROVIDERS = frozenset(
        {"chatgpt", "claude", "gemini", "grok", "deepseek", "perplexity"}
    )

    def __init__(self, project_root: Path) -> None:
        project_root = Path(project_root).resolve()
        if project_root.name != "ai-collaboration":
            project_root = project_root / "ai-collaboration"
        self.project_root = project_root
        self.browser = BrowserAutomationSession()

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
                "transport": "embedded-browser-view",
                "uses_api_key": False,
                "memory_candidates": [],
            }
        result = await self.browser.send_prompt(agent, str(task.get("content") or ""))
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
            "transport": "embedded-browser-view",
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
        return await self.browser.open_agent(agent)

    def _available_browser(self) -> BrowserAutomationSession:
        return self.browser

    async def authorize_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        requested_provider = str(agent.get("provider") or "").strip().casefold()
        if requested_provider not in self.BROWSER_PRIMARY_PROVIDERS:
            return {"ok": False, "message": "UNSUPPORTED_PROVIDER_AUTHORIZATION"}
        result = await self.open_agent(agent)
        return {
            **result,
            "authorization_provider": f"{requested_provider}-browser-session",
            "mode": "embedded-browser-view",
            "uses_api_key": False,
            "interactive_browser": True,
            "automatic_inference_fallback": None,
        }

    def provider_status(self) -> list[dict[str, Any]]:
        return [
            {
                "provider": provider,
                "mode": "embedded-browser-view",
                "installed": True,
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
        return {
            "product": "embedded-browser-view",
            "available": True,
            "ready_without_restart": True,
            "mode": "embedded-browser-view",
            "automation": True,
            "foreground": True,
            "shared_browser_context": True,
        }

    async def close_background_context(self) -> None:
        return None

    async def shutdown(self) -> None:
        await self.browser.shutdown()
