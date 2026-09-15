"""Split from consolidated test_ai_collaboration.py."""
from __future__ import annotations

from _ai_collaboration_test_support import *  # noqa: F401,F403


def test_default_agents_include_perplexity(tmp_path: Path) -> None:
    repository = AiCollaborationRepository(tmp_path)
    agents = {agent["agent_id"]: agent for agent in repository.list_agents()}
    assert agents["perplexity"]["name"] == "Perplexity"
    assert agents["perplexity"]["provider"] == "perplexity"
    assert agents["perplexity"]["home_url"] == "https://www.perplexity.ai/"


def test_google_search_is_available_without_joining_general_group_by_default(
    tmp_path: Path,
) -> None:
    repository = AiCollaborationRepository(tmp_path)
    agents = {agent["agent_id"]: agent for agent in repository.list_agents()}

    assert agents["google-search"]["provider"] == "google-search"
    assert agents["google-search"]["selected"] == 0


def test_general_and_investment_urls_are_saved_separately(tmp_path: Path) -> None:
    repository = AiCollaborationRepository(tmp_path)

    saved = repository.save_agent_business_settings(
        "perplexity",
        general_url="https://www.perplexity.ai/",
        investment_url="https://www.perplexity.ai/finance/",
        general_enabled=True,
        investment_enabled=False,
        business_capabilities=["general", "advanced_search", "calculation"],
    )

    assert saved["general_url"] == "https://www.perplexity.ai/"
    assert saved["investment_url"] == "https://www.perplexity.ai/finance/"
    assert saved["general_enabled"] == 1
    assert saved["investment_enabled"] == 0
    with pytest.raises(ValueError, match="https"):
        repository.save_agent_business_settings(
            "perplexity",
            general_url="http://localhost:3000/",
            investment_url="https://www.perplexity.ai/finance/",
            general_enabled=True,
            investment_enabled=True,
            business_capabilities=["general"],
        )


def test_chatgpt_star_training_url_is_saved_separately(tmp_path: Path) -> None:
    repository = AiCollaborationRepository(tmp_path)

    saved = repository.save_agent_business_settings(
        "chatgpt",
        general_url="https://chatgpt.com/",
        investment_url="https://chatgpt.com/finance/",
        star_training_url="https://chatgpt.com/g/star-training",
        general_enabled=True,
        investment_enabled=True,
        business_capabilities=["general"],
    )

    assert saved["star_training_url"] == "https://chatgpt.com/g/star-training"


class _FakeBrowserSession:
    def __init__(self) -> None:
        self.opened_agent: dict[str, object] | None = None
        self.sent_providers: list[str] = []
        self.sent_agents: list[dict[str, object]] = []

    async def open_agent(self, agent: dict[str, object]) -> dict[str, object]:
        self.opened_agent = dict(agent)
        return {"ok": True, "url": agent["home_url"]}

    async def send_prompt(
        self, agent: dict[str, object], prompt: str
    ) -> dict[str, str]:
        self.sent_agents.append(dict(agent))
        self.sent_providers.append(str(agent["provider"]))
        if agent["provider"] == "gemini":
            assert "Gemini 支援的 Google" in prompt
            return {"status": "completed", "content": "Gemini 已整理", "error": ""}
        assert agent["provider"] == "chatgpt"
        assert "最終統籌" in prompt
        return {"status": "completed", "content": "ChatGPT 最終統籌", "error": ""}

    async def close_background_context(self) -> None:
        return None


class _FakeProviderSession:
    def __init__(self) -> None:
        self.tasks: list[dict[str, object]] = []
        self.providers: list[str] = []

    async def send_task(
        self, agent: dict[str, object], task: dict[str, object]
    ) -> dict[str, object]:
        self.tasks.append(dict(task))
        self.providers.append(str(agent["provider"]))
        return {
            "status": "completed",
            "provider": "chatgpt",
            "content": "已整合",
            "transport": "embedded-browser-view",
            "fallback": {
                "used": True,
                "requested_provider": agent["provider"],
                "effective_provider": "chatgpt",
            },
            "memory_candidates": [
                {
                    "candidate_id": "candidate-1",
                    "kind": "longform",
                    "title": "摘要",
                    "content": "已整合",
                    "status": "candidate",
                    "source_agent_id": agent["agent_id"],
                    "direct_database_write": False,
                }
            ],
        }

    async def close_background_context(self) -> None:
        return None


def test_service_routes_investment_business_to_investment_url(tmp_path: Path) -> None:
    session = _FakeBrowserSession()
    service = AiCollaborationService(tmp_path, session=session)
    service.repository.save_agent_business_settings(
        "perplexity",
        general_url="https://www.perplexity.ai/",
        investment_url="https://www.perplexity.ai/finance/",
        general_enabled=True,
        investment_enabled=True,
        business_capabilities=["general", "advanced_search", "calculation"],
    )

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_open_agent",
            {"agent_id": "perplexity", "business_scope": "investment"},
        )
    )

    assert result["ok"] is True
    assert session.opened_agent is not None
    assert session.opened_agent["home_url"] == "https://www.perplexity.ai/finance/"
    assert session.opened_agent["business_scope"] == "investment"


def test_star_training_routes_chatgpt_to_its_dedicated_conversation_url(
    tmp_path: Path,
) -> None:
    session = _FakeBrowserSession()
    service = AiCollaborationService(tmp_path, session=session)
    service.repository.save_agent_business_settings(
        "chatgpt",
        general_url="https://chatgpt.com/",
        investment_url="https://chatgpt.com/finance/",
        star_training_url="https://chatgpt.com/g/star-training",
        general_enabled=True,
        investment_enabled=True,
        business_capabilities=["general"],
    )

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "請建立星澄訓練候選。",
                "business_task": "training-candidate-authoring",
                "business_scope": "general",
                "_authorized_requester_actor": "governance/tool/xingcheng",
            },
        )
    )

    assert result["ok"] is True
    assert session.sent_agents[0]["home_url"] == "https://chatgpt.com/g/star-training"
    assert session.sent_agents[0]["conversation_scope"] == "star-training"

    _event, general_result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "請協助星澄整理這項需求。",
                "business_task": "general",
                "business_scope": "general",
                "_authorized_requester_actor": "governance/tool/xingcheng",
            },
        )
    )

    assert general_result["ok"] is True
    assert session.sent_agents[-1]["home_url"] == "https://chatgpt.com/"
    assert "conversation_scope" not in session.sent_agents[-1]


def test_default_business_roles_match_governed_routing(tmp_path: Path) -> None:
    repository = AiCollaborationRepository(tmp_path)
    agents = {agent["agent_id"]: agent for agent in repository.list_agents()}

    assert "search" in agents["gemini"]["business_capabilities"]
    assert {"advanced_search", "calculation"} <= set(
        agents["perplexity"]["business_capabilities"]
    )
    assert "longform" in agents["claude"]["business_capabilities"]
    assert "reasoning" in agents["deepseek"]["business_capabilities"]
    assert {"social_media", "trends", "breaking_news"} <= set(
        agents["grok"]["business_capabilities"]
    )
    assert {"comprehensive", "orchestration"} <= set(
        agents["chatgpt"]["business_capabilities"]
    )
    assert "orchestration" not in agents["grok"]["business_capabilities"]


def test_google_results_are_processed_by_gemini_before_returning_to_star(
    tmp_path: Path,
) -> None:
    session = _FakeBrowserSession()
    service = AiCollaborationService(tmp_path, session=session)

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "2330 配息 股價 淨值 官方",
                "agent_ids": ["google-search", "gemini"],
                "business_scope": "investment",
                "research_pipeline": "google-gemini",
            },
        )
    )

    assert result["ok"] is True
    assert result["research_pipeline"] == "google-gemini"
    group = result["group_message"]
    assert group["google_raw_results_exposed"] is False
    assert group["processor"] == "gemini"
    assert {item["agent_id"] for item in group["responses"]} == {"gemini", "chatgpt"}
    assert next(item for item in group["responses"] if item["agent_id"] == "gemini")["content"] == "Gemini 已整理"
    assert result["final_response"]["content"] == "ChatGPT 最終統籌"
    assert session.sent_providers == ["gemini", "chatgpt"]


def test_official_cli_task_carries_star_memory_and_returns_candidates_only(
    tmp_path: Path,
) -> None:
    session = _FakeProviderSession()
    service = AiCollaborationService(tmp_path, session=session)

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "整理內容",
                "agent_ids": ["claude"],
                "business_scope": "general",
                "business_task": "longform",
                "memory_context": [
                    {
                        "memory_id": "m1",
                        "kind": "instruction",
                        "title": "偏好",
                        "content": "使用繁體中文",
                        "origin_model_id": "star-main-native-model",
                        "business_scope": "general",
                    }
                ],
            },
        )
    )

    assert result["ok"] is True
    assert session.tasks[0]["requested_by"] == "xingcheng"
    assert session.tasks[0]["memory_context"][0]["content"] == "使用繁體中文"
    assert result["memory_interchange"]["direct_database_access"] is False
    assert result["memory_interchange"]["candidate_count"] == 2
    response = result["group_message"]["responses"][0]
    assert response["execution_provider"] == "chatgpt"
    assert response["fallback"]["used"] is True


def test_investment_manager_cannot_request_external_ai_directly(tmp_path: Path) -> None:
    service = AiCollaborationService(tmp_path, session=_FakeProviderSession())

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "越權要求",
                "agent_ids": ["chatgpt"],
                "business_scope": "investment",
                "_authorized_requester_actor": "governance/tool/ai-assistant",
            },
        )
    )

    assert result["ok"] is False
    assert result["message"] == "PERMISSION_DENIED"


def test_tool_general_mode_runs_only_selected_agents_in_parallel(tmp_path: Path) -> None:
    session = _FakeProviderSession()
    service = AiCollaborationService(tmp_path, session=session)

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "一般協作需求",
                "agent_ids": ["claude", "gemini"],
                "business_scope": "general",
                "business_task": "general",
                "_authorized_requester_actor": "governance/tool/ai-collaboration",
            },
        )
    )

    assert result["ok"] is True
    assert result["execution_mode"] == "parallel-user-selected-general"
    assert result["selected_agents"] == ["claude", "gemini"]
    assert result["final_coordinator"] is None
    assert result["requested_by"] == "ai-collaboration"
    assert session.providers == ["claude", "gemini"]
    assert all(task["task_type"] == "general" for task in session.tasks)
    assert all(
        task["memory_policy"]["writeback"] == "disabled"
        for task in session.tasks
    )


def test_tool_general_mode_cannot_start_star_fixed_workflow(tmp_path: Path) -> None:
    service = AiCollaborationService(tmp_path, session=_FakeProviderSession())

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "嘗試指定固定任務",
                "agent_ids": ["deepseek"],
                "business_task": "reasoning",
                "_authorized_requester_actor": "governance/tool/ai-collaboration",
            },
        )
    )

    assert result == {"ok": False, "message": "PERMISSION_DENIED"}


class _BrowserAwaitingSession:
    async def send_task(
        self, agent: dict[str, object], _task: dict[str, object]
    ) -> dict[str, object]:
        return {
            "status": "awaiting-user",
            "provider": agent["provider"],
            "content": "",
            "error": "",
            "error_code": "FOREGROUND_BROWSER_INTERACTION_REQUIRED",
            "transport": "embedded-browser-view",
            "memory_candidates": [],
            "fallback": {"used": False, "browser_only": True},
        }

    async def close_background_context(self) -> None:
        return None


def test_tool_general_mode_returns_immediately_for_browser_results(
    tmp_path: Path,
) -> None:
    service = AiCollaborationService(tmp_path, session=_BrowserAwaitingSession())
    actor = "governance/tool/ai-collaboration"
    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "瀏覽器協作需求",
                "agent_ids": ["chatgpt"],
                "business_task": "general",
                "_authorized_requester_actor": actor,
            },
        )
    )

    response = result["group_message"]["responses"][0]
    assert result["workflow_status"] == "awaiting-browser-results"
    assert response["status"] == "awaiting-user"

    _event, completed = asyncio.run(
        service.handle(
            "ai_nexus_complete_browser_response",
            {
                "message_id": result["group_message"]["message_id"],
                "agent_id": "chatgpt",
                "content": "瀏覽器回覆",
                "_authorized_requester_actor": actor,
            },
        )
    )

    assert completed["ok"] is True
    saved = completed["messages"][0]["responses"][0]
    assert saved["status"] == "completed"
    assert saved["content"] == "瀏覽器回覆"
    assert saved["transport"] == "embedded-browser-view"
