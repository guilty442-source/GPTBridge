from __future__ import annotations

import asyncio
import json
from typing import Any

from shared_layer.embedded_browser_client import (
    EmbeddedBrowserClient,
    InProcessEmbeddedBrowser,
)

from .provider_adapters import (
    ADAPTER_VERSION,
    VERIFICATION_MARKERS,
    ProviderAdapter,
    adapter_for,
)


# Backwards-compatible aliases for callers that imported the selector maps
# from this module before the provider-adapter registry was extracted.
def _registry_selectors(field: str) -> dict[str, tuple[str, ...]]:
    from .provider_adapters import PROVIDER_ADAPTERS

    return {
        key: getattr(adapter, field) for key, adapter in PROVIDER_ADAPTERS.items()
    }


INPUT_SELECTORS = _registry_selectors("input_selectors")
SEND_SELECTORS = _registry_selectors("send_selectors")
RESPONSE_SELECTORS = _registry_selectors("response_selectors")


class BrowserAutomationSession:
    """Drive provider websites using the embedded Electron BrowserView.

    Sessions live inside the tool's own Electron window (published through
    the tool-window browser bridge) so automation is visible, shares the
    user's authenticated session, and keeps working while the main-system
    window is closed.  No external browser process is launched.
    """

    RESPONSE_TIMEOUT_SECONDS = 120
    PAGE_LOAD_WAIT_SECONDS = 30
    RESPONSE_STABLE_POLLS = 3
    OWNER_MODULE = "ai-collaboration"

    def __init__(self, profile_root: Any = None, chrome_executable: Any = None) -> None:
        # profile_root and chrome_executable are accepted for backward
        # compatibility but no longer used — the embedded browser manages
        # its own state inside Electron.
        self._client = EmbeddedBrowserClient()
        self._fallback = InProcessEmbeddedBrowser()
        self._sessions: dict[str, str] = {}  # agent_id → session_id
        self._init_lock = asyncio.Lock()
        self._last_backend = "embedded-browser"

    def _browser(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """Dispatch to the Electron IPC client, falling back to the
        in-process browser when the embedded-browser bridge is
        unavailable (headless / test environments)."""
        result = getattr(self._client, method)(*args, **kwargs)
        bridge_down = result is None or (
            isinstance(result, dict)
            and result.get("ok") is False
            and result.get("message") == "EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE"
        )
        if bridge_down:
            self._last_backend = "in-process"
            return getattr(self._fallback, method)(*args, **kwargs)
        self._last_backend = "embedded-browser"
        return result

    def _bridge_kind(self) -> str:
        """Which bridge currently serves sessions: tool-window / main-system /
        in-process fallback / unknown (injected test clients)."""
        if self._last_backend == "in-process":
            return "in-process"
        identity = getattr(self._client, "bridge_identity", None)
        if not callable(identity):
            return "unknown"
        return str(identity().get("kind") or "unavailable")

    def session_state(self, agent_id: str) -> dict[str, Any]:
        """Cheap registry probe: session presence + current URL."""
        session_id = self._sessions.get(str(agent_id or ""))
        if not session_id:
            return {"session_state": "closed", "login_state": "unknown"}
        url = self._get_url(session_id)
        if url is None:
            return {"session_state": "expired", "login_state": "unknown"}
        return {
            "session_state": "open",
            "login_state": "unknown",
            "url": url,
            "backend": self._bridge_kind(),
        }

    async def open_agent(self, agent: dict[str, Any]) -> dict[str, Any]:
        try:
            session_id = await self._ensure_agent_session(agent)
            provider = str(agent.get("provider") or "").strip().casefold()
            return {
                "ok": True,
                "url": agent.get("home_url", ""),
                "mode": "embedded-browser-view",
                "automation": self._bridge_kind() != "in-process",
                "foreground": True,
                "open_target": "embedded-browser-view",
                "shared_browser_context": True,
                "session_id": session_id,
                "provider_identity": provider,
                "adapter_version": ADAPTER_VERSION,
            }
        except Exception as exc:
            return self._browser_failure(exc)

    async def send_prompt(
        self, agent: dict[str, Any], prompt: str
    ) -> dict[str, Any]:
        provider = str(agent.get("provider") or "").strip().casefold()
        adapter = adapter_for(provider)
        try:
            session_id = await self._ensure_agent_session(agent)
            page_failure = await self._ensure_provider_page(
                session_id, agent, adapter, provider
            )
            if page_failure is not None:
                return page_failure
            marker = await self._detect_verification(session_id)
            if marker:
                return self._verification_result(provider, marker, submitted=False)
            early = await self._submit_prompt(
                session_id, provider, prompt, adapter
            )
            if early is not None:
                return early

            state, content, marker = await self._capture_response(
                session_id, adapter
            )
            if marker:
                return self._verification_result(provider, marker, submitted=True)
            if state == "response_completed" and content:
                return self._completed_result(provider, content, session_id, state)
            return self._capture_required_result(provider, state, session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return self._prompt_failure(provider, exc)

    async def _ensure_provider_page(
        self,
        session_id: str,
        agent: dict[str, Any],
        adapter: ProviderAdapter | None,
        provider: str,
    ) -> dict[str, Any] | None:
        """Guarantee the session is on the provider's own host.

        Prevents mixing responses after the user switched providers: the
        input is only ever filled on the registered provider page.
        """
        target_url = str(agent.get("home_url") or "").strip()
        if not target_url.startswith("https://"):
            return self._failed_result(provider, "INVALID_EXTERNAL_URL")
        if adapter is None:
            return None
        current = self._get_url(session_id) or ""
        if adapter.matches_host(current):
            return None
        navigation = self._browser("navigate", session_id, target_url)
        if isinstance(navigation, dict) and navigation.get("ok") is False:
            return self._failed_result(
                provider,
                "EMBEDDED_BROWSER_SESSION_FAILED",
                str(navigation.get("message") or ""),
            )
        if not await self._wait_page_ready(session_id):
            return self._failed_result(
                provider,
                "EMBEDDED_BROWSER_SESSION_FAILED",
                "provider page did not finish loading",
            )
        return None

    async def _wait_page_ready(self, session_id: str) -> bool:
        probe = "(() => ({ ready: document.readyState === 'complete' }))()"
        deadline = self.PAGE_LOAD_WAIT_SECONDS * 2
        for _ in range(deadline):
            result = await self._execute_script(session_id, probe)
            if result.get("ok") and bool(
                (result.get("result") or {}).get("ready")
            ):
                return True
            await asyncio.sleep(0.5)
        return False

    async def _submit_prompt(
        self,
        session_id: str,
        provider: str,
        prompt: str,
        adapter: ProviderAdapter | None,
    ) -> dict[str, Any] | None:
        """Fill input and submit via JavaScript execution.

        Returns an early failure/awaiting result, or None when submitted.
        """
        input_selectors = (
            adapter.input_selectors
            if adapter is not None
            else ("textarea", '[contenteditable="true"]')
        )
        send_selectors = (
            adapter.send_selectors
            if adapter is not None
            else ('button[type="submit"]',)
        )

        fill_script = f"""
            (() => {{
                const selectors = {json.dumps(list(input_selectors))};
                let input = null;
                for (const sel of selectors) {{
                    input = document.querySelector(sel);
                    if (input) break;
                }}
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

        # The embedded view returns from create_session before the page has
        # finished loading; retry the fill until the input appears or the
        # bounded wait is exhausted (login walls keep reporting not-found).
        found = False
        for _ in range(self.PAGE_LOAD_WAIT_SECONDS):
            result = await self._execute_script(session_id, fill_script)
            found = bool(result.get("ok")) and bool(
                (result.get("result") or {}).get("found")
            )
            if found:
                break
            await asyncio.sleep(1)
        if not found:
            error_code = "BROWSER_LOGIN_OR_INPUT_REQUIRED"
            return self._waiting_result(provider, error_code, submitted=False)

        submit_script = f"""
            (() => {{
                const selectors = {json.dumps(list(send_selectors))};
                for (const sel of selectors) {{
                    const btn = document.querySelector(sel);
                    if (btn && !btn.disabled) {{ btn.click(); return {{ sent: true }}; }}
                }}
                // Last resort: composer inputs submit on Enter.
                const inputSelectors = {json.dumps(list(input_selectors))};
                for (const sel of inputSelectors) {{
                    const input = document.querySelector(sel);
                    if (!input) continue;
                    input.focus();
                    for (const type of ['keydown', 'keypress', 'keyup']) {{
                        input.dispatchEvent(new KeyboardEvent(type, {{
                            key: 'Enter', code: 'Enter', keyCode: 13,
                            which: 13, bubbles: true, cancelable: true,
                        }}));
                    }}
                    return {{ sent: true, via: 'enter-key' }};
                }}
                return {{ sent: false }};
            }})()
        """
        submit_result = await self._execute_script(session_id, submit_script)
        sent = submit_result.get("ok") and (submit_result.get("result") or {}).get("sent")

        if not sent:
            return self._waiting_result(
                provider, "BROWSER_SEND_CONTROL_NOT_FOUND", submitted=False
            )
        return None

    async def _capture_response(
        self, session_id: str, adapter: ProviderAdapter | None
    ) -> tuple[str, str, str | None]:
        """Provider Response Adapter state machine.

        States: response_started / response_streaming / response_completed /
        response_failed / response_timeout.  Completion is decided by a
        stable response body with no generating indicator — never by a
        fixed sleep.
        """
        probe = self._response_probe_script(adapter)
        previous = ""
        stable = 0
        for _ in range(self.RESPONSE_TIMEOUT_SECONDS):
            result = await self._execute_script(session_id, probe)
            if result.get("ok"):
                page_marker = await self._detect_verification(session_id)
                if page_marker:
                    return "response_failed", "", page_marker
                payload = result.get("result")
                if isinstance(payload, dict):
                    if payload.get("verification"):
                        return (
                            "response_failed",
                            "",
                            str(payload.get("marker") or "verification"),
                        )
                    generating = bool(payload.get("generating"))
                    content = str(payload.get("content") or "").strip()
                    if generating:
                        stable = 0
                        previous = content or previous
                    elif content:
                        if content != previous:
                            previous = content
                            stable = 0
                        else:
                            stable += 1
                            if (
                                stable >= self.RESPONSE_STABLE_POLLS
                                and len(content) > 10
                            ):
                                return "response_completed", content, None
            await asyncio.sleep(1)
        return "response_timeout", previous, None

    def _response_probe_script(self, adapter: ProviderAdapter | None) -> str:
        response_selectors = list(
            adapter.response_selectors
            if adapter is not None
            else ('[class*="assistant" i]',)
        )
        generating_selectors = list(
            adapter.generating_selectors if adapter is not None else ()
        )
        markers = json.dumps(list(VERIFICATION_MARKERS))
        return f"""
            (() => {{
                const respSels = {json.dumps(response_selectors)};
                let content = '';
                for (const sel of respSels) {{
                    const els = document.querySelectorAll(sel);
                    if (!els.length) continue;
                    const last = els[els.length - 1];
                    const text = last.innerText || last.textContent || '';
                    if (text.trim()) {{ content = text; break; }}
                }}
                const genSels = {json.dumps(generating_selectors)};
                let generating = false;
                for (const sel of genSels) {{
                    const el = document.querySelector(sel);
                    if (el && el.offsetParent !== null) {{ generating = true; break; }}
                }}
                const text = ((document.title || '') + ' ' +
                    (document.body ? document.body.innerText || '' : ''))
                    .toLowerCase();
                for (const marker of {markers}) {{
                    if (text.includes(marker)) {{
                        return {{ content: '', generating: false,
                            verification: true, marker }};
                    }}
                }}
                return {{ content, generating, verification: false, marker: '' }};
            }})()
        """

    @staticmethod
    def _extract_script(provider: str) -> str:
        response_selector = RESPONSE_SELECTORS.get(provider, ('[class*="assistant" i]',))[0]
        return f"""
            (() => {{
                const el = document.querySelector({response_selector!r});
                if (!el) return {{ content: '' }};
                return {{ content: el.textContent || el.innerText || '' }};
            }})()
        """

    def _completed_result(
        self,
        provider: str,
        content: str,
        session_id: str,
        response_state: str = "response_completed",
    ) -> dict[str, Any]:
        return {
            "status": "completed",
            "provider": provider,
            "content": content,
            "error": "",
            "error_code": "",
            "transport": "embedded-browser-view",
            "uses_api_key": False,
            "memory_candidates": [],
            "adapter_version": ADAPTER_VERSION,
            "response_state": response_state,
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
                "response_state": response_state,
            },
        }

    def _capture_required_result(
        self, provider: str, response_state: str, session_id: str
    ) -> dict[str, Any]:
        """Degraded path: submission landed but reliable capture is
        unavailable — offer manual result import, never fake success."""
        return {
            "status": "awaiting-user",
            "provider": provider,
            "content": "",
            "error": "BROWSER_RESPONSE_CAPTURE_REQUIRED",
            "error_code": "BROWSER_RESPONSE_CAPTURE_REQUIRED",
            "transport": "embedded-browser-view",
            "uses_api_key": False,
            "memory_candidates": [],
            "adapter_version": ADAPTER_VERSION,
            "response_state": response_state,
            "fallback": {
                "used": False,
                "browser_only": True,
                "cross_provider_substitution": False,
            },
            "browser_handoff": {
                "url": self._get_url(session_id) or "",
                "submitted": True,
                "send_method": "embedded-js-click",
                "response_captured": False,
                "response_state": response_state,
                "manual_import_available": True,
            },
        }

    def _prompt_failure(self, provider: str, exc: Exception) -> dict[str, Any]:
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
            "adapter_version": ADAPTER_VERSION,
            "fallback": {
                "used": False,
                "browser_only": True,
                "cross_provider_substitution": False,
            },
        }

    async def shutdown(self) -> None:
        for agent_id, session_id in list(self._sessions.items()):
            self._browser("close", session_id)
        self._sessions.clear()

    async def _ensure_agent_session(self, agent: dict[str, Any]) -> str:
        agent_id = str(agent.get("agent_id") or "").strip()
        target_url = str(agent.get("home_url") or "").strip()
        if not agent_id or not target_url.startswith("https://"):
            raise ValueError("INVALID_BROWSER_AGENT")

        async with self._init_lock:
            existing = self._sessions.get(agent_id)
            if existing:
                if self._get_url(existing) is not None:
                    return existing
                # The view was closed (window refresh / expired session):
                # drop the stale id and recreate exactly once.
                self._sessions.pop(agent_id, None)

            kind = self._bridge_kind()
            if kind == "main-system":
                # Sessions on the main-system bridge are invisible and do
                # not share the tool window's authenticated state — refuse
                # instead of silently automating a browser nobody can see.
                raise RuntimeError("EMBEDDED_BROWSER_SESSION_FAILED")

            result = self._browser(
                "create_session",
                owner_module=self.OWNER_MODULE,
                url=target_url,
                session_id=f"ai-collaboration-{agent_id}",
            )
            if not result.get("ok"):
                raise RuntimeError(
                    str(result.get("message") or "EMBEDDED_BROWSER_SESSION_FAILED")
                )
            session_id = str(result["id"])
            self._sessions[agent_id] = session_id
            return session_id

    async def _execute_script(self, session_id: str, script: str) -> dict[str, Any]:
        return self._browser("execute_script", session_id, script)

    def _get_url(self, session_id: str) -> str | None:
        return self._browser("get_url", session_id)

    @staticmethod
    def _verification_detect_script() -> str:
        markers = json.dumps(list(VERIFICATION_MARKERS))
        return f"""
            /* __verify_markers__ */
            (() => {{
                const text = ((document.title || "") + " " +
                    (document.body ? document.body.innerText || "" : ""))
                    .toLowerCase();
                for (const marker of {markers}) {{
                    if (text.includes(marker)) return {{ flagged: true, marker }};
                }}
                return {{ flagged: false, marker: "" }};
            }})()
        """

    async def _detect_verification(self, session_id: str) -> str | None:
        """Inspect page title/body for captcha/Cloudflare challenge markers."""
        result = await self._execute_script(
            session_id, self._verification_detect_script()
        )
        if not result.get("ok"):
            return None
        payload = result.get("result")
        if isinstance(payload, dict) and payload.get("flagged"):
            return str(payload.get("marker") or "verification")
        return None

    @staticmethod
    def _verification_result(
        provider: str,
        marker: str,
        *,
        submitted: bool,
    ) -> dict[str, Any]:
        return {
            "status": "waiting_verification",
            "provider": provider,
            "content": "",
            "error": f"BROWSER_VERIFICATION_REQUIRED:{marker}",
            "error_code": "BROWSER_VERIFICATION_REQUIRED",
            "transport": "embedded-browser-view",
            "uses_api_key": False,
            "memory_candidates": [],
            "adapter_version": ADAPTER_VERSION,
            "response_state": "response_failed",
            "fallback": {
                "used": False,
                "browser_only": True,
                "cross_provider_substitution": False,
            },
            "browser_handoff": {
                "submitted": submitted,
                "response_captured": False,
                "verification_marker": marker,
            },
        }

    @staticmethod
    def _failed_result(
        provider: str, error_code: str, detail: str = ""
    ) -> dict[str, Any]:
        return {
            "status": "failed",
            "provider": provider,
            "content": "",
            "error": f"{error_code}:{detail}" if detail else error_code,
            "error_code": error_code,
            "transport": "embedded-browser-view",
            "uses_api_key": False,
            "memory_candidates": [],
            "adapter_version": ADAPTER_VERSION,
            "fallback": {
                "used": False,
                "browser_only": True,
                "cross_provider_substitution": False,
            },
        }

    @staticmethod
    def _browser_failure(exc: Exception) -> dict[str, Any]:
        message = str(exc)
        if "EMBEDDED_BROWSER_SESSION_FAILED" in message:
            return {
                "ok": False,
                "error_code": "EMBEDDED_BROWSER_SESSION_FAILED",
                "message": message,
            }
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
            "adapter_version": ADAPTER_VERSION,
            "fallback": {
                "used": False,
                "browser_only": True,
                "cross_provider_substitution": False,
            },
            "browser_handoff": {
                "submitted": submitted,
                "response_captured": False,
                "manual_import_available": True,
            },
        }
