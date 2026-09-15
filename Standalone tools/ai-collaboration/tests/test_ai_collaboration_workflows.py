"""Split from consolidated test_ai_collaboration.py."""
from __future__ import annotations

from _ai_collaboration_test_support import *  # noqa: F401,F403


def test_star_fixed_task_plan_ignores_manual_agent_rerouting(tmp_path: Path) -> None:
    session = _FakeProviderSession()
    service = AiCollaborationService(tmp_path, session=session)

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "進行深度推理",
                "agent_ids": ["gemini", "claude"],
                "business_task": "reasoning",
                "business_scope": "general",
            },
        )
    )

    assert result["fixed_task_owner"] == "deepseek"
    assert result["requested_selection_ignored_for_routing"] == ["gemini", "claude"]
    assert session.providers == ["deepseek", "chatgpt"]
    assert result["final_coordinator"] == "chatgpt"
    assert result["final_response"]["agent_id"] == "chatgpt"


def test_star_can_run_all_fixed_ai_workflows_concurrently(tmp_path: Path) -> None:
    class ParallelSession(_FakeProviderSession):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.maximum_active = 0

        async def send_task(
            self, agent: dict[str, object], task: dict[str, object]
        ) -> dict[str, object]:
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            try:
                await asyncio.sleep(0.02)
                return await super().send_task(agent, task)
            finally:
                self.active -= 1

    session = ParallelSession()
    service = AiCollaborationService(tmp_path, session=session)
    tasks = [
        {"task_type": "general", "content": "一般"},
        {"task_type": "search", "content": "搜尋"},
        {"task_type": "longform", "content": "長文"},
        {"task_type": "reasoning", "content": "推理"},
        {"task_type": "social_media", "content": "社群"},
        {"task_type": "advanced_search", "content": "高階搜尋"},
    ]

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {"tasks": tasks, "business_scope": "general"},
        )
    )

    assert result["ok"] is True
    assert result["task_count"] == 6
    assert result["maximum_parallel_tasks"] == 6
    assert session.maximum_active == 6
    assert {item["fixed_task_owner"] for item in result["results"]} == {
        "chatgpt",
        "gemini",
        "claude",
        "deepseek",
        "grok",
        "perplexity",
    }
    assert all(item["final_coordinator"] == "chatgpt" for item in result["results"])


def test_browser_waits_three_cycles_then_uses_chatgpt_terminal_fallback(
    tmp_path: Path,
) -> None:
    class WaitingSession:
        def __init__(self) -> None:
            self.fallback_calls = 0

        async def send_task(
            self, agent: dict[str, object], _task: dict[str, object]
        ) -> dict[str, object]:
            if agent["provider"] == "chatgpt":
                return {
                    "status": "completed",
                    "provider": "chatgpt",
                    "content": "ChatGPT 最終統籌",
                    "memory_candidates": [],
                }
            return {
                "status": "awaiting-user",
                "provider": agent["provider"],
                "content": "",
                "memory_candidates": [],
            }

        async def send_terminal_fallback(
            self,
            _agent: dict[str, object],
            _task: dict[str, object],
            reason: str,
        ) -> dict[str, object]:
            self.fallback_calls += 1
            assert reason == "BROWSER_WAIT_EXHAUSTED_AFTER_THREE_ATTEMPTS"
            return {
                "status": "completed",
                "provider": "chatgpt",
                "content": "ChatGPT 接手原任務",
                "memory_candidates": [],
                "fallback": {
                    "used": True,
                    "terminal_fallback_only": True,
                    "wait_attempts": 3,
                },
            }

        async def close_background_context(self) -> None:
            return None

    session = WaitingSession()
    service = AiCollaborationService(tmp_path, session=session)
    service.BROWSER_WAIT_SECONDS = 0.01

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "處理長文",
                "business_task": "longform",
                "business_scope": "general",
            },
        )
    )

    assert session.fallback_calls == 1
    claude = next(
        item for item in result["group_message"]["responses"]
        if item["agent_id"] == "claude"
    )
    assert claude["execution_provider"] == "chatgpt"
    assert claude["fallback"]["terminal_fallback_only"] is True
    assert result["final_response"]["content"] == "ChatGPT 最終統籌"


def test_provider_session_uses_managed_embedded_browser(tmp_path: Path) -> None:
    tool_root = tmp_path / "ai-collaboration"
    tool_root.mkdir()
    session = AiCollaborationProviderSession(tool_root)
    opened: list[str] = []

    class FakeBrowser:
        async def open_agent(self, agent: dict[str, object]) -> dict[str, object]:
            opened.append(str(agent["agent_id"]))
            return {
                "ok": True,
                "url": agent["home_url"],
                "foreground": True,
                "automation": True,
                "open_target": "shared-foreground-browser-tab",
                "shared_browser_context": True,
            }

    session.browser = FakeBrowser()  # type: ignore[assignment]

    result = asyncio.run(
        session.open_agent(
            {
                "agent_id": "gemini",
                "provider": "gemini",
                "home_url": "https://gemini.google.com/",
            }
        )
    )

    assert result["ok"] is True
    assert result["foreground"] is True
    assert result["automation"] is True
    assert result["open_target"] == "shared-foreground-browser-tab"
    assert result["shared_browser_context"] is True
    assert opened == ["gemini"]


def test_provider_session_uses_embedded_browser(tmp_path: Path) -> None:
    session = AiCollaborationProviderSession(tmp_path)

    browser = session._available_browser()

    assert browser is not None
    assert session.browser_status()["ready_without_restart"] is True
    assert session.browser_status()["product"] == "embedded-browser-view"


def test_all_provider_authorization_uses_embedded_browser_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = AiCollaborationProviderSession(tmp_path)
    opened: list[str] = []

    async def fake_open_agent(agent: dict[str, object]) -> dict[str, object]:
        opened.append(str(agent["provider"]))
        return {"ok": True, "url": "https://example.com/"}

    monkeypatch.setattr(session, "open_agent", fake_open_agent)
    for provider in sorted(session.BROWSER_PRIMARY_PROVIDERS):
        result = asyncio.run(
            session.authorize_agent(
                {"provider": provider, "home_url": "https://example.com/"}
            )
        )
        assert result["ok"] is True
        assert result["mode"] == "embedded-browser-view"
        assert result["automatic_inference_fallback"] is None

    assert opened == sorted(session.BROWSER_PRIMARY_PROVIDERS)
