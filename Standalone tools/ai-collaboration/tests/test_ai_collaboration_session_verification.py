"""EC-2 session/login: page-level verification detection, waiting_verification
status propagation, paste-back recovery, per-agent session reuse, and
response timeout -> awaiting-user handoff."""
from __future__ import annotations

from _ai_collaboration_test_support import *  # noqa: F401,F403

from ai_collaboration.integration.browser_automation import BrowserAutomationSession


class _StubBrowserCore:
    """In-process browser core stub keyed off the script being executed."""

    def __init__(
        self,
        *,
        flagged: bool = False,
        marker: str = "",
        content: str = "",
    ) -> None:
        self.flagged = flagged
        self.marker = marker
        self.content = content
        self.create_calls = 0
        self.sessions: dict[str, str] = {}

    def create_session(
        self,
        owner_module: str,
        url: str,
        bounds: dict[str, int] | None = None,
    ) -> dict[str, object]:
        self.create_calls += 1
        session_id = f"{owner_module}-stub{self.create_calls}"
        self.sessions[session_id] = url
        return {"ok": True, "id": session_id, "url": url}

    def execute_script(
        self, session_id: str, script: str, args: tuple = ()
    ) -> dict[str, object]:
        if "__verify_markers__" in script:
            return {
                "ok": True,
                "result": {"flagged": self.flagged, "marker": self.marker},
            }
        if "sent" in script:
            return {"ok": True, "result": {"sent": True}}
        if "found" in script:
            return {"ok": True, "result": {"found": True}}
        return {"ok": True, "result": {"content": self.content}}

    def get_url(self, session_id: str) -> str | None:
        return self.sessions.get(session_id)

    def close(self, session_id: str) -> dict[str, object]:
        self.sessions.pop(session_id, None)
        return {"ok": True}


class _BridgeDownClient:
    """EmbeddedBrowserClient stand-in: bridge always unavailable."""

    def create_session(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"ok": False, "message": "EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE"}

    def execute_script(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"ok": False, "message": "EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE"}

    def get_url(self, *args: object, **kwargs: object) -> None:
        return None

    def close(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"ok": False, "message": "EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE"}


_AGENT = {
    "agent_id": "gemini",
    "provider": "gemini",
    "home_url": "https://gemini.google.com/",
}


def test_browser_detects_verification_challenge_on_page(tmp_path: Path) -> None:
    session = BrowserAutomationSession()
    core = _StubBrowserCore(flagged=True, marker="cloudflare")
    session._fallback = core  # type: ignore[assignment]
    session._client = _BridgeDownClient()  # type: ignore[assignment]

    result = asyncio.run(session.send_prompt(_AGENT, "整理資料"))

    assert result["status"] == "waiting_verification"
    assert result["error_code"] == "BROWSER_VERIFICATION_REQUIRED"
    assert result["browser_handoff"]["verification_marker"] == "cloudflare"
    assert result["transport"] == "embedded-browser-view"
    assert result["uses_api_key"] is False


def test_browser_marks_verification_detected_mid_response(tmp_path: Path) -> None:
    session = BrowserAutomationSession()
    core = _StubBrowserCore(content="")
    session._fallback = core  # type: ignore[assignment]
    session._client = _BridgeDownClient()  # type: ignore[assignment]
    session.RESPONSE_TIMEOUT_SECONDS = 3

    async def scenario() -> dict[str, object]:
        async def flag_after_submit() -> None:
            await asyncio.sleep(1.2)
            core.flagged = True
            core.marker = "captcha"

        flagger = asyncio.ensure_future(flag_after_submit())
        try:
            return await session.send_prompt(_AGENT, "整理資料")
        finally:
            await flagger

    result = asyncio.run(scenario())

    assert result["status"] == "waiting_verification"
    assert result["error_code"] == "BROWSER_VERIFICATION_REQUIRED"
    assert result["browser_handoff"]["submitted"] is True
    assert result["browser_handoff"]["verification_marker"] == "captcha"


def test_browser_response_timeout_falls_back_to_awaiting_user(
    tmp_path: Path,
) -> None:
    session = BrowserAutomationSession()
    session._fallback = _StubBrowserCore(content="")  # type: ignore[assignment]
    session._client = _BridgeDownClient()  # type: ignore[assignment]
    session.RESPONSE_TIMEOUT_SECONDS = 1

    result = asyncio.run(session.send_prompt(_AGENT, "整理資料"))

    assert result["status"] == "awaiting-user"
    assert result["error_code"] == "BROWSER_RESPONSE_CAPTURE_REQUIRED"
    assert result["browser_handoff"]["submitted"] is True


def test_agent_sessions_are_reused_per_provider(tmp_path: Path) -> None:
    session = BrowserAutomationSession()
    core = _StubBrowserCore()
    session._fallback = core  # type: ignore[assignment]
    session._client = _BridgeDownClient()  # type: ignore[assignment]

    async def scenario() -> None:
        await session.open_agent(_AGENT)
        await session.open_agent(_AGENT)
        await session.open_agent(
            {"agent_id": "claude", "provider": "claude", "home_url": "https://claude.ai/"}
        )

    asyncio.run(scenario())

    assert core.create_calls == 2


class _VerificationProviderSession:
    """Provider session stub returning waiting_verification for the owner."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def send_task(
        self, agent: dict[str, object], _task: dict[str, object]
    ) -> dict[str, object]:
        provider = str(agent["provider"])
        self.calls.append(provider)
        if provider == "chatgpt":
            return {
                "status": "completed",
                "provider": "chatgpt",
                "content": "ChatGPT 最終統籌",
                "transport": "embedded-browser-view",
                "memory_candidates": [],
            }
        return {
            "status": "waiting_verification",
            "provider": provider,
            "content": "",
            "error": "BROWSER_VERIFICATION_REQUIRED:captcha",
            "error_code": "BROWSER_VERIFICATION_REQUIRED",
            "transport": "embedded-browser-view",
            "memory_candidates": [],
            "fallback": {"used": False, "browser_only": True},
        }

    async def close_background_context(self) -> None:
        return None


def test_waiting_verification_surfaces_attention_and_accepts_paste_back(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        session = _VerificationProviderSession()
        service = AiCollaborationService(tmp_path, session=session)
        service.BROWSER_WAIT_SECONDS = 30

        _event, result = await service.handle(
            "ai_nexus_send_message",
            {
                "content": "整理最新搜尋結果",
                "business_task": "search",
                "business_scope": "general",
            },
        )

        gemini = next(
            item
            for item in result["group_message"]["responses"]
            if item["agent_id"] == "gemini"
        )
        assert gemini["status"] == "waiting_verification"
        assert gemini["error_code"] == "BROWSER_VERIFICATION_REQUIRED"
        message_id = result["group_message"]["message_id"]

        _event, state = await service.handle("ai_nexus_get_state", {})
        assert state["diagnostics"]["state"] == "attention"
        assert state["diagnostics"]["collaboration"]["waiting_verification"] == 1

        _event, completed = await service.handle(
            "ai_nexus_complete_browser_response",
            {
                "message_id": message_id,
                "agent_id": "gemini",
                "content": "驗證後的瀏覽器回覆",
                "_authorized_requester_actor": "governance/tool/ai-collaboration",
            },
        )

        assert completed["ok"] is True
        saved = next(
            item
            for item in completed["messages"][-1]["responses"]
            if item["agent_id"] == "gemini"
        )
        assert saved["status"] == "completed"
        assert saved["content"] == "驗證後的瀏覽器回覆"

    asyncio.run(scenario())
