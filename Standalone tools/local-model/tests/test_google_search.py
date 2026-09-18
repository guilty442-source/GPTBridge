"""Split from consolidated test_xingcheng.py (local-model/tests/test_google_search.py)."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401
from _xingcheng_test_support import ROOT

import sys
from pathlib import Path
from xingcheng.integration.external_research import ExternalBrowserResearch

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]

from xingcheng.integration.external_research import ExternalBrowserResearch


class _FakeChannelClient:
    def __init__(self) -> None:
        self.call: tuple[str, str, dict[str, object], int] | None = None

    def request_sync(
        self,
        target: str,
        command: str,
        payload: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        self.call = (target, command, payload, timeout_seconds)
        return {
            "ok": True,
            "group_message": {
                "responses": [
                    {
                        "agent_id": "google-search",
                        "status": "completed",
                        "content": '{"provider":"google-search","results":[]}',
                        "error": "",
                    }
                ]
            },
        }


def test_star_requests_investment_google_only_through_ai_channel() -> None:
    research = ExternalBrowserResearch()
    client = _FakeChannelClient()
    research._client = client

    result = research.search(
        [{"symbol": "2330", "name": "台積電", "message": "not found"}]
    )

    assert client.call is not None
    target, command, payload, timeout = client.call
    assert target == "ai-collaboration"
    assert command == "ai_nexus_send_message"
    assert payload["agent_ids"] == ["google-search", "gemini"]
    assert payload["business_scope"] == "investment"
    assert payload["research_pipeline"] == "google-gemini"
    assert '"2330 台積電" 配息 股價 淨值 官方' in str(payload["content"])
    assert timeout == 200
    assert result["recipient"] == "xingcheng"
    assert result["provider"] == "google-search"
    assert result["processor"] == "gemini"
    assert result["business_scope"] == "investment"
    assert result["result_role"] == "discovery-only"


def test_chatgpt_final_coordination_returns_only_to_star() -> None:
    research = ExternalBrowserResearch()
    client = _FakeChannelClient()

    def request_sync(
        target: str,
        command: str,
        payload: dict[str, object],
        *,
        timeout_seconds: int,
    ) -> dict[str, object]:
        client.call = (target, command, payload, timeout_seconds)
        return {
            "ok": True,
            "workflow_status": "completed",
            "final_response": {
                "agent_id": "chatgpt",
                "status": "completed",
                "content": "統籌完成",
                "error": "",
            },
            "memory_interchange": {
                "direct_database_access": False,
                "candidates": [],
            },
        }

    client.request_sync = request_sync  # type: ignore[method-assign]
    research._client = client

    result = research.coordinate_investment_analysis(
        {"portfolio": {"active_holding_count": 125}, "facts_locked": True}
    )

    assert client.call is not None
    target, command, payload, timeout = client.call
    assert target == "ai-collaboration"
    assert command == "ai_nexus_send_message"
    assert payload["agent_ids"] == ["chatgpt"]
    assert payload["business_task"] == "orchestration"
    assert result["ok"] is True
    assert result["provider"] == "chatgpt"
    assert result["recipient"] == "xingcheng"
    assert result["content"] == "統籌完成"
    assert result["direct_database_access"] is False
    assert timeout == 200


def test_repair_research_uses_governed_network_channel() -> None:
    research = ExternalBrowserResearch()
    client = _FakeChannelClient()
    research._client = client

    result = research.search_repair_solutions(
        error_class="ModuleNotFoundError",
        error_message="No module named example",
        failure_code="STARTUP_DEPENDENCY_EXCEPTION",
        component="main-system",
        runtime_versions={"python": "3.11"},
    )

    assert client.call is not None
    target, command, payload, timeout = client.call
    assert target == "ai-collaboration"
    assert command == "ai_nexus_send_message"
    # ai-collaboration's accepted vocabulary: business_scope is limited to
    # {general, investment} and business_task to _FIXED_TASK_CAPABILITIES;
    # "search" dispatches the gemini fixed-owner (Google-backed retrieval in
    # the embedded browser). The google-gemini pipeline is fenced to
    # investment scope and "google-search" is a retired agent id, so neither
    # may appear here.
    assert payload["business_scope"] == "general"
    assert payload["business_task"] == "search"
    assert "research_pipeline" not in payload
    assert "agent_ids" not in payload
    assert payload["execution_allowed"] is False
    assert payload["direct_database_write"] is False
    assert result["result_role"] == "unverified-repair-candidates"
    assert timeout == 200



########################################################################
