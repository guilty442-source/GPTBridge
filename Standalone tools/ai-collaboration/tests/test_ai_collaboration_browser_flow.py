"""Offline Provider Adapter tests with a scripted in-process page.

Simulates the whole provider flow without any real website: open, input,
send, streaming response, capture, save, reload, cancel, timeout,
website layout change and expired session.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
SERVICES_ROOT = TOOL_ROOT / "src" / "backend" / "services"
if str(SERVICES_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICES_ROOT))

from ai_collaboration.application.service import AiCollaborationService
from ai_collaboration.infrastructure.repository import AiCollaborationRepository
from ai_collaboration.integration.browser_automation import (
    BrowserAutomationSession,
)
from ai_collaboration.integration.provider_session import (
    AiCollaborationProviderSession,
)


class SimulatedPage:
    """A scripted DOM standing in for one provider page."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.ready = True
        self.has_input = True
        self.has_send = True
        self.verify_marker = ""
        self.generating = False
        self.pending_chunks: list[str] = []
        self.content = ""
        self.prompt = ""


class ScriptedBrowser:
    """EmbeddedBrowserClient-compatible fake driving SimulatedPage objects."""

    def __init__(self) -> None:
        self.pages: dict[str, SimulatedPage] = {}
        self.navigated: list[tuple[str, str]] = []
        self.closed: list[str] = []

    def _page(self, session_id: str) -> SimulatedPage | None:
        return self.pages.get(session_id)

    def create_session(
        self,
        owner_module: str,
        url: str,
        bounds: dict | None = None,
        *,
        session_id: str | None = None,
    ) -> dict:
        sid = session_id or f"{owner_module}-x"
        if sid not in self.pages:
            self.pages[sid] = SimulatedPage(url)
        return {"ok": True, "id": sid, "url": self.pages[sid].url}

    def navigate(self, session_id: str, url: str) -> dict:
        page = self._page(session_id)
        if page is None:
            return {"ok": False, "message": "SESSION_NOT_FOUND"}
        page.url = url
        self.navigated.append((session_id, url))
        return {"ok": True}

    def execute_script(self, session_id: str, script: str, args: tuple = ()) -> dict:
        page = self._page(session_id)
        if page is None:
            return {"ok": False, "message": "SESSION_NOT_FOUND"}
        if "__verify_markers__" in script:
            return {
                "ok": True,
                "result": {
                    "flagged": bool(page.verify_marker),
                    "marker": page.verify_marker,
                },
            }
        if "document.readyState" in script:
            return {"ok": True, "result": {"ready": page.ready}}
        if "found" in script and "selectors" in script:
            if page.has_input:
                page.prompt = script
                return {"ok": True, "result": {"found": True}}
            return {"ok": True, "result": {"found": False}}
        if "sent" in script:
            if page.has_send:
                page.generating = True
                return {"ok": True, "result": {"sent": True}}
            return {"ok": True, "result": {"sent": False}}
        if "generating" in script and "verification" in script:
            if page.verify_marker:
                return {
                    "ok": True,
                    "result": {
                        "content": "",
                        "generating": False,
                        "verification": True,
                        "marker": page.verify_marker,
                    },
                }
            if page.pending_chunks:
                page.content = page.pending_chunks.pop(0)
            generating = bool(page.pending_chunks) or page.generating
            if not page.pending_chunks:
                page.generating = False
            return {
                "ok": True,
                "result": {
                    "content": page.content,
                    "generating": generating,
                    "verification": False,
                    "marker": "",
                },
            }
        return {"ok": True, "result": None}

    def show(self, session_id: str) -> dict:
        return {"ok": True}

    def hide(self, session_id: str) -> dict:
        return {"ok": True}

    def close(self, session_id: str) -> dict:
        self.pages.pop(session_id, None)
        self.closed.append(session_id)
        return {"ok": True}

    def resize(self, session_id: str, bounds: dict) -> dict:
        return {"ok": True}

    def get_url(self, session_id: str):
        page = self._page(session_id)
        return page.url if page else None

    def list_sessions(self):
        return [{"id": sid, "url": p.url} for sid, p in self.pages.items()]

    def close_module_sessions(self, owner_module: str) -> int:
        before = len(self.pages)
        self.pages = {
            sid: p for sid, p in self.pages.items() if owner_module not in sid
        }
        return before - len(self.pages)


def _agent(provider: str = "chatgpt") -> dict:
    return {
        "agent_id": provider,
        "provider": provider,
        "home_url": (
            "https://chatgpt.com/"
            if provider == "chatgpt"
            else f"https://{provider}.example.com/"
        ),
    }


def _session(fake: ScriptedBrowser) -> BrowserAutomationSession:
    session = BrowserAutomationSession()
    session._client = fake
    session.RESPONSE_TIMEOUT_SECONDS = 6
    session.PAGE_LOAD_WAIT_SECONDS = 2
    return session


def test_open_agent_creates_session_on_provider_url():
    async def run() -> None:
        fake = ScriptedBrowser()
        session = _session(fake)
        result = await session.open_agent(_agent())
        assert result["ok"] is True
        assert result["session_id"] == "ai-collaboration-chatgpt"
        assert fake.pages[result["session_id"]].url == "https://chatgpt.com/"

    asyncio.run(run())


def test_send_prompt_completes_after_streaming():
    async def run() -> None:
        fake = ScriptedBrowser()
        session = _session(fake)
        await session.open_agent(_agent())
        page = fake.pages["ai-collaboration-chatgpt"]
        page.pending_chunks = ["回答中", "回答中，請稍候", "完整的繁體中文回覆內容"]
        result = await session.send_prompt(_agent(), "請介紹 GPTBridge")
        assert result["status"] == "completed"
        assert result["content"] == "完整的繁體中文回覆內容"
        assert result["response_state"] == "response_completed"
        assert result["error_code"] == ""

    asyncio.run(run())


def test_send_prompt_timeout_degrades_to_manual_import():
    async def run() -> None:
        fake = ScriptedBrowser()
        session = _session(fake)
        session.RESPONSE_TIMEOUT_SECONDS = 3
        await session.open_agent(_agent())
        # generating forever: capture can never confirm completion
        fake.pages["ai-collaboration-chatgpt"].generating = True
        result = await session.send_prompt(_agent(), "hi")
        assert result["status"] == "awaiting-user"
        assert result["error_code"] == "BROWSER_RESPONSE_CAPTURE_REQUIRED"
        assert result["response_state"] == "response_timeout"
        assert result["browser_handoff"]["manual_import_available"] is True

    asyncio.run(run())


def test_send_prompt_missing_input_reports_login_required():
    async def run() -> None:
        fake = ScriptedBrowser()
        session = _session(fake)
        await session.open_agent(_agent())
        fake.pages["ai-collaboration-chatgpt"].has_input = False
        result = await session.send_prompt(_agent(), "hi")
        assert result["status"] == "awaiting-user"
        assert result["error_code"] == "BROWSER_LOGIN_OR_INPUT_REQUIRED"

    asyncio.run(run())


def test_send_prompt_missing_send_control_reports_layout_change():
    async def run() -> None:
        fake = ScriptedBrowser()
        session = _session(fake)
        await session.open_agent(_agent())
        fake.pages["ai-collaboration-chatgpt"].has_send = False
        result = await session.send_prompt(_agent(), "hi")
        assert result["status"] == "awaiting-user"
        assert result["error_code"] == "BROWSER_SEND_CONTROL_NOT_FOUND"

    asyncio.run(run())


def test_send_prompt_verification_marker_waits_for_user():
    async def run() -> None:
        fake = ScriptedBrowser()
        session = _session(fake)
        await session.open_agent(_agent())
        fake.pages["ai-collaboration-chatgpt"].verify_marker = "captcha"
        result = await session.send_prompt(_agent(), "hi")
        assert result["status"] == "waiting_verification"
        assert result["error_code"] == "BROWSER_VERIFICATION_REQUIRED"

    asyncio.run(run())


def test_expired_session_is_recreated_once():
    async def run() -> None:
        fake = ScriptedBrowser()
        session = _session(fake)
        await session.open_agent(_agent())
        fake.pages.clear()  # window refresh destroyed the view
        result = await session.send_prompt(_agent(), "hi")
        assert "ai-collaboration-chatgpt" in fake.pages
        assert result["status"] in {"awaiting-user", "completed"}

    asyncio.run(run())


def test_wrong_host_navigates_back_to_provider_page():
    async def run() -> None:
        fake = ScriptedBrowser()
        session = _session(fake)
        await session.open_agent(_agent())
        page = fake.pages["ai-collaboration-chatgpt"]
        page.url = "https://unrelated.example.com/"
        page.pending_chunks = ["這是一段足夠長的最終回覆內容，供擷取驗證"]
        await session.send_prompt(_agent(), "hi")
        assert (
            "ai-collaboration-chatgpt",
            "https://chatgpt.com/",
        ) in fake.navigated

    asyncio.run(run())


def test_unsupported_provider_is_rejected():
    async def run() -> None:
        provider_session = AiCollaborationProviderSession(TOOL_ROOT)
        result = await provider_session.send_task(
            {
                "agent_id": "x",
                "provider": "not-a-provider",
                "home_url": "https://x.example.com/",
            },
            {"content": "hi"},
        )
        assert result["status"] == "failed"
        assert result["error_code"] == "UNSUPPORTED_BROWSER_PROVIDER"

    asyncio.run(run())


def test_service_send_marks_cancelled_on_cancel(tmp_path: Path):
    async def run() -> None:
        repo_root = tmp_path / "ai-collaboration"
        repo_root.mkdir(parents=True)
        (repo_root / "manifest.json").write_text("{}", encoding="utf-8")

        fake = ScriptedBrowser()
        browser = _session(fake)
        browser.PAGE_LOAD_WAIT_SECONDS = 1
        browser.RESPONSE_TIMEOUT_SECONDS = 3

        provider_session = AiCollaborationProviderSession(TOOL_ROOT)
        provider_session.browser = browser
        service = AiCollaborationService(repo_root, session=provider_session)

        payload = {
            "_authorized_requester_actor": "governance/tool/ai-collaboration",
            "_governed_request_id": "req-cancel-1",
            "request_id": "req-cancel-1",
            "content": "請用繁體中文簡短介紹 GPTBridge。",
            "agent_ids": ["chatgpt"],
            "business_scope": "general",
            "business_task": "general",
        }
        # Page never finishes generating → send stays in flight.
        fake_send = asyncio.create_task(
            service.handle("ai_nexus_send_message", dict(payload))
        )
        await asyncio.sleep(0.3)
        cancelled = await service.cancel("req-cancel-1")
        fake_send.cancel()
        await asyncio.gather(fake_send, return_exceptions=True)
        assert cancelled is True

        repository = AiCollaborationRepository(repo_root)
        messages = repository.list_messages()
        assert len(messages) == 1
        message = messages[0]
        assert message["request_id"] == "req-cancel-1"
        assert message["runtime_generation"] == service.runtime_generation
        response = message["responses"][0]
        assert response["status"] == "cancelled"
        assert response["error_code"] == "REQUEST_CANCELLED"
        assert response["completed_at"]
        assert response["runtime_generation"] == service.runtime_generation

    asyncio.run(run())


def test_service_send_dedupes_idempotency_key(tmp_path: Path):
    async def run() -> None:
        repo_root = tmp_path / "ai-collaboration"
        repo_root.mkdir(parents=True)
        (repo_root / "manifest.json").write_text("{}", encoding="utf-8")

        fake = ScriptedBrowser()
        browser = _session(fake)
        provider_session = AiCollaborationProviderSession(TOOL_ROOT)
        provider_session.browser = browser
        service = AiCollaborationService(repo_root, session=provider_session)

        payload = {
            "_authorized_requester_actor": "governance/tool/ai-collaboration",
            "request_id": "req-dedupe-1",
            "idempotency_key": "dedupe-key-1",
            "content": "請用繁體中文簡短介紹 GPTBridge。",
            "agent_ids": ["chatgpt"],
            "business_scope": "general",
            "business_task": "general",
        }
        _event, first = await service.handle(
            "ai_nexus_send_message", dict(payload)
        )
        assert first.get("ok") is True
        _event2, second = await service.handle(
            "ai_nexus_send_message", dict(payload)
        )
        # One click must never send twice: the second identical request
        # returns the recorded result without a new group message.
        assert second.get("deduplicated") is True
        repository = AiCollaborationRepository(repo_root)
        assert len(repository.list_messages()) == 1

    asyncio.run(run())
