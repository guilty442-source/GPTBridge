from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "local-model" / "src" / "backend" / "services"))

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
