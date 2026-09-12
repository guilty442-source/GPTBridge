from __future__ import annotations

import asyncio
import re
from typing import Any

from shared_layer.embedded_browser_client import (
    EmbeddedBrowserClient,
    InProcessEmbeddedBrowser,
)


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
    """Drive provider websites using the embedded Electron BrowserView.

    Replaces the old Playwright + external Chrome approach with IPC calls
    to the Electron embedded browser module.  No external browser process
    is launched — all browsing happens inside the main Electron window.
    """

    RESPONSE_TIMEOUT_SECONDS = 120

    def __init__(self, profile_root: Any = None, chrome_executable: Any = None) -> None:
        # profile_root and chrome_executable are accepted for backward
        # compatibility but no longer used — the embedded browser manages
        # its own state inside Electron.
        self._client = EmbeddedBrowserClient()
        self._fallback = InProcessEmbeddedBrowser()
        self._sessions: dict[str, str] = {}  # agent_id → session_id
        self._init_lock = asyncio.Lock()

    async def open_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        try:
            session_id = await self._ensure_agent_session(agent)
            return {
                "ok": True,
                "url": agent.get("home_url", ""),
                "mode": "embedded-browser-view",
                "automation": True,
                "foreground": True,
                "open_target": "embedded-browser-view",
                "shared_browser_context": True,
                "session_id": session_id,
            }
        except Exception as exc:
            return self._browser_failure(exc)

    async def send_prompt(
        self, agent: dict[str, Any], prompt: str
    ) -> dict[str, Any]:
        provider = str(agent.get("provider") or "").strip().casefold()
        try:
            session_id = await self._ensure_agent_session(agent)

            # Fill input and submit via JavaScript execution
            input_selector = INPUT_SELECTORS.get(
                provider, ("textarea", '[contenteditable="true"]')
            )[0]
            send_selector = SEND_SELECTORS.get(
                provider, ('button[type="submit"]',)
            )[0]

            fill_script = f"""
                (() => {{
                    const input = document.querySelector({input_selector!r});
                    if (!input) return {{ found: false }};
                    input.focus();
                    if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {{
                        input.value = {prompt!r};
                        input.dispatchEvent(new Event('input', {{ bubbles: true }}));
                    }} else {{
                        input.textContent = {prompt!r};
                        input.dispatchEvent(new InputEvent('input', {{ bubbles: true }}));
                    }}
                    return {{ found: true }};
                }})()
            """

            result = await self._execute_script(session_id, fill_script)
            if not result.get("ok") or not result.get("result", {}).get("found"):
                error_code = "BROWSER_LOGIN_OR_INPUT_REQUIRED"
                return self._waiting_result(provider, error_code, submitted=False)

            submit_script = f"""
                (() => {{
                    const btn = document.querySelector({send_selector!r});
                    if (!btn) return {{ sent: false }};
                    btn.click();
                    return {{ sent: true }};
                }})()
            """
            submit_result = await self._execute_script(session_id, submit_script)
            sent = submit_result.get("ok") and submit_result.get("result", {}).get("sent")

            if not sent:
                return self._waiting_result(
                    provider, "BROWSER_SEND_CONTROL_NOT_FOUND", submitted=False
                )

            # Wait for response
            response_selector = RESPONSE_SELECTORS.get(provider, ('[class*="assistant" i]',))[0]
            extract_script = f"""
                (() => {{
                    const el = document.querySelector({response_selector!r});
                    if (!el) return {{ content: '' }};
                    return {{ content: el.textContent || el.innerText || '' }};
                }})()
            """

            content = await self._wait_for_response(session_id, extract_script)
            if not content:
                return self._waiting_result(
                    provider, "BROWSER_RESPONSE_CAPTURE_REQUIRED", submitted=True
                )

            return {
                "status": "completed",
                "provider": provider,
                "content": content,
                "error": "",
                "error_code": "",
                "transport": "embedded-browser-view",
                "uses_api_key": False,
                "memory_candidates": [],
                "fallback": {
                    "used": False,
                    "browser_only": True,
                    "cross_provider_substitution": False,
                },
                "browser_handoff": {
                    "url": self._get_url(session_id) or "",
                    "submitted": True,
                    "send_method": "embedded-js-click",
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
                "transport": "embedded-browser-view",
                "uses_api_key": False,
                "memory_candidates": [],
                "fallback": {
                    "used": False,
                    "browser_only": True,
                    "cross_provider_substitution": False,
                },
            }

    async def shutdown(self) -> None:
        for agent_id, session_id in list(self._sessions.items()):
            self._fallback.close(session_id)
        self._sessions.clear()

    async def _ensure_agent_session(self, agent: dict[str, Any]) -> str:
        agent_id = str(agent.get("agent_id") or "").strip()
        target_url = str(agent.get("home_url") or "").strip()
        if not agent_id or not target_url.startswith("https://"):
            raise ValueError("INVALID_BROWSER_AGENT")

        async with self._init_lock:
            existing = self._sessions.get(agent_id)
            if existing:
                return existing

            result = self._fallback.create_session(
                owner_module="ai-collaboration",
                url=target_url,
            )
            if not result.get("ok"):
                raise RuntimeError("EMBEDDED_BROWSER_SESSION_FAILED")
            session_id = str(result["id"])
            self._sessions[agent_id] = session_id
            return session_id

    async def _execute_script(self, session_id: str, script: str) -> dict[str, Any]:
        # Use in-process fallback for now; in production this goes through IPC
        return self._fallback.execute_script(session_id, script)

    def _get_url(self, session_id: str) -> str | None:
        return self._fallback.get_url(session_id)

    async def _wait_for_response(
        self, session_id: str, extract_script: str
    ) -> str:
        for _ in range(self.RESPONSE_TIMEOUT_SECONDS):
            result = await self._execute_script(session_id, extract_script)
            if result.get("ok"):
                content = str(result.get("result", {}).get("content") or "").strip()
                if content and len(content) > 10:
                    return content
            await asyncio.sleep(1)
        return ""

    @staticmethod
    def _browser_failure(exc: Exception) -> dict[str, Any]:
        message = str(exc)
        if any(marker in message.lower() for marker in VERIFICATION_MARKERS):
            return {
                "ok": False,
                "error_code": "BROWSER_VERIFICATION_REQUIRED",
                "message": message,
            }
        return {
            "ok": False,
            "error_code": "BROWSER_ERROR",
            "message": message,
        }

    @staticmethod
    def _waiting_result(
        provider: str,
        error_code: str,
        *,
        submitted: bool,
    ) -> dict[str, Any]:
        return {
            "status": "awaiting-user",
            "provider": provider,
            "content": "",
            "error": error_code,
            "error_code": error_code,
            "transport": "embedded-browser-view",
            "uses_api_key": False,
            "memory_candidates": [],
            "fallback": {
                "used": False,
                "browser_only": True,
                "cross_provider_substitution": False,
            },
            "browser_handoff": {
                "submitted": submitted,
                "response_captured": False,
            },
        }
