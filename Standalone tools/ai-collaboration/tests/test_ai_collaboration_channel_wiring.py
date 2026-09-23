"""EC-1 channel wiring: composer send -> per-AI status chain -> embedded
browser wait -> user paste-back -> send returns -> state refresh.

Covers the in-flight completion path where ``ai_nexus_send_message`` is
blocked inside ``_wait_for_browser_or_fallback`` and the governed
``ai_nexus_complete_browser_response`` command resolves the pending future,
plus the ``ai_nexus_get_state`` status refresh at each step.
"""
from __future__ import annotations

from _ai_collaboration_test_support import *  # noqa: F401,F403


class _BrowserAwaitingFixedSession:
    """Fixed owner awaits the foreground browser; ChatGPT coordinator completes."""

    def __init__(self) -> None:
        self.sent_providers: list[str] = []

    async def send_task(
        self, agent: dict[str, object], _task: dict[str, object]
    ) -> dict[str, object]:
        provider = str(agent["provider"])
        self.sent_providers.append(provider)
        if provider == "chatgpt":
            return {
                "status": "completed",
                "provider": "chatgpt",
                "content": "ChatGPT 最終統籌",
                "transport": "embedded-browser-view",
                "memory_candidates": [],
            }
        return {
            "status": "awaiting-user",
            "provider": provider,
            "content": "",
            "error": "",
            "error_code": "FOREGROUND_BROWSER_INTERACTION_REQUIRED",
            "transport": "embedded-browser-view",
            "memory_candidates": [],
            "fallback": {"used": False, "browser_only": True},
        }

    async def close_background_context(self) -> None:
        return None


def _latest_responses(state: dict[str, object]) -> list[dict[str, object]]:
    messages = state.get("messages") or []
    if not messages:
        return []
    return [
        item
        for item in messages[-1].get("responses", [])
        if isinstance(item, dict)
    ]


def test_send_waits_for_browser_then_paste_back_completes_and_state_refreshes(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = AiCollaborationService(
            tmp_path, session=_BrowserAwaitingFixedSession()
        )
        service.BROWSER_WAIT_SECONDS = 30

        send = asyncio.ensure_future(
            service.handle(
                "ai_nexus_send_message",
                {
                    "content": "整理最新搜尋結果",
                    "business_task": "search",
                    "business_scope": "general",
                },
            )
        )

        message_id = ""
        waiting: dict[str, object] | None = None
        for _ in range(200):
            await asyncio.sleep(0.01)
            _event, state = await service.handle("ai_nexus_get_state", {})
            responses = _latest_responses(state)
            gemini = next(
                (item for item in responses if item.get("agent_id") == "gemini"),
                None,
            )
            if isinstance(gemini, dict) and gemini.get("status") == "awaiting-user":
                message_id = str(state["messages"][-1]["message_id"])
                waiting = gemini
                break
        assert waiting is not None
        assert waiting["error_code"] == "FOREGROUND_BROWSER_INTERACTION_REQUIRED"
        assert waiting["transport"] == "embedded-browser-view"
        assert waiting["fallback"]["browser_only"] is True
        assert waiting["fallback"]["chatgpt_terminal_fallback"] is False
        assert not send.done()

        _event, completed = await service.handle(
            "ai_nexus_complete_browser_response",
            {
                "message_id": message_id,
                "agent_id": "gemini",
                "content": "Gemini 瀏覽器回覆內容",
                "_authorized_requester_actor": "governance/tool/ai-collaboration",
            },
        )
        assert completed["ok"] is True
        assert completed["message"] == "已接收瀏覽器結果，固定任務流程將繼續處理。"

        _event, result = await asyncio.wait_for(send, timeout=10)
        assert result["ok"] is True
        assert result["workflow_status"] == "completed"
        gemini = next(
            item
            for item in result["group_message"]["responses"]
            if item["agent_id"] == "gemini"
        )
        assert gemini["status"] == "completed"
        assert gemini["content"] == "Gemini 瀏覽器回覆內容"
        assert gemini["transport"] == "embedded-browser-view"
        assert gemini["fallback"]["cross_provider_substitution"] is False
        assert result["final_response"]["agent_id"] == "chatgpt"
        assert result["final_response"]["content"] == "ChatGPT 最終統籌"

        candidates = result["memory_interchange"]["candidates"]
        assert any(
            item.get("source_agent_id") == "gemini"
            and "Gemini 瀏覽器回覆內容" in str(item.get("content") or "")
            for item in candidates
        )
        assert result["memory_interchange"]["direct_database_access"] is False

        _event, refreshed = await service.handle("ai_nexus_get_state", {})
        latest_responses = _latest_responses(refreshed)
        assert latest_responses
        assert all(
            item.get("status") == "completed" for item in latest_responses
        )
        assert (
            refreshed["diagnostics"]["collaboration"]["latest_message_id"]
            == message_id
        )

    asyncio.run(scenario())


def test_complete_browser_response_rejects_unauthorized_actor(
    tmp_path: Path,
) -> None:
    service = AiCollaborationService(tmp_path, session=_BrowserAwaitingFixedSession())

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_complete_browser_response",
            {
                "message_id": "any",
                "agent_id": "gemini",
                "content": "偽造回覆",
                "_authorized_requester_actor": "governance/tool/investment-mobile",
            },
        )
    )

    assert result == {"ok": False, "message": "PERMISSION_DENIED", "error_code": "PERMISSION_DENIED"}


def test_complete_browser_response_without_waiting_task_fails_closed(
    tmp_path: Path,
) -> None:
    service = AiCollaborationService(tmp_path, session=_BrowserAwaitingFixedSession())

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_complete_browser_response",
            {
                "message_id": "missing-message",
                "agent_id": "gemini",
                "content": "延遲送達的結果",
                "_authorized_requester_actor": "governance/tool/ai-collaboration",
            },
        )
    )

    assert result["ok"] is False
    assert result["message"] == "目前沒有等待中的瀏覽器任務"


def test_complete_browser_response_rejects_blank_content(tmp_path: Path) -> None:
    service = AiCollaborationService(tmp_path, session=_BrowserAwaitingFixedSession())

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_complete_browser_response",
            {
                "message_id": "any",
                "agent_id": "gemini",
                "content": "   ",
                "_authorized_requester_actor": "governance/tool/ai-collaboration",
            },
        )
    )

    assert result == {"ok": False, "message": "瀏覽器結果不可空白", "error_code": "REQUEST_REJECTED"}
