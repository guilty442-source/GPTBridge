from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SHARED_SRC = ROOT / "shared-layer" / "src"
AI_COLLABORATION_SERVICES = (
    ROOT / "ai-collaboration" / "src" / "backend" / "services"
)
for path in (ROOT, SHARED_SRC, AI_COLLABORATION_SERVICES):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from governance_rule.permission_directory.registries.permissions.tool_routes import (  # noqa: E402
    XINGCHENG_AUTOMATIC_WORKFLOW_SEQUENCE,
    ai_channel_status,
    authorize_ai_route,
    authorize_ai_target,
    authorize_xingcheng_automatic_workflow,
)
from ai_collaboration.domain.task_protocol import build_ai_task_envelope  # noqa: E402
from ai_collaboration.integration.provider_session import (  # noqa: E402
    AiCollaborationProviderSession,
)


def test_only_star_can_request_external_collaboration() -> None:
    assert (
        authorize_ai_route(
            "governance/tool/xingcheng",
            "ai-collaboration",
            "ai_nexus_send_message",
        )
        == "xingcheng"
    )
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(
            "governance/tool/ai-assistant",
            "ai-collaboration",
            "ai_nexus_send_message",
        )


def test_investment_manager_can_only_request_star_commands() -> None:
    assert (
        authorize_ai_route(
            "governance/tool/ai-assistant",
            "xingcheng",
            "xingcheng_analyze_investments",
        )
        == "ai-assistant"
    )
    assert (
        authorize_ai_route(
            "governance/tool/ai-assistant",
            "xingcheng",
            "xingcheng_manage_investment_accounting",
        )
        == "ai-assistant"
    )
    assert (
        authorize_ai_route(
            "governance/tool/ai-assistant",
            "xingcheng",
            "xingcheng_discuss_investment_analysis",
        )
        == "ai-assistant"
    )
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(
            "governance/tool/ai-assistant",
            "xingcheng",
            "ai_nexus_send_message",
        )


def test_external_ai_cannot_route_a_response_to_investment_manager() -> None:
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(
            "governance/tool/ai-collaboration",
            "ai-assistant",
            "investment_ai_consult",
        )


def test_target_accepts_only_the_governed_route() -> None:
    authorize_ai_target(
        "governance/tool/xingcheng",
        "ai-collaboration",
        "ai_nexus_send_message",
    )
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_target(
            "governance/tool/ai-assistant",
            "ai-collaboration",
            "ai_nexus_send_message",
        )


def test_governance_can_manage_target_without_becoming_ai_participant() -> None:
    authorize_ai_target(
        "governance/main-system",
        "xingcheng",
        "xingcheng_status",
    )
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(
            "governance/main-system",
            "ai-collaboration",
            "ai_nexus_send_message",
        )


def test_status_declares_governance_and_star_authority() -> None:
    status = ai_channel_status()
    assert status["channel_id"] == "shared-layer/ai-channel"
    assert status["highest_authority"] == "governance-rule"
    assert status["channel_top_level_tool"] == "xingcheng"
    assert status["external_ai_response_recipient"] == "xingcheng"
    assert status["investment_manager_external_ai"] is False
    assert status["xingcheng_automatic_workflow"]["excluded_path_roots"] == [
        "governance_rule"
    ]


def test_governance_validates_the_xingcheng_automatic_workflow() -> None:
    payload = {
        "automatic_workflow": True,
        "autonomous_agent": True,
        "workflow_sequence": list(XINGCHENG_AUTOMATIC_WORKFLOW_SEQUENCE),
        "primary_language": "zh-TW",
    }
    authorize_xingcheng_automatic_workflow(
        "governance/tool/star-chat",
        "xingcheng",
        "xingcheng_infer",
        payload,
    )
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_xingcheng_automatic_workflow(
            "governance/tool/star-chat",
            "xingcheng",
            "xingcheng_infer",
            {**payload, "workflow_sequence": ["execute", "result"]},
        )


def test_star_chat_has_only_status_and_inference_routes() -> None:
    assert authorize_ai_route(
        "governance/tool/star-chat", "xingcheng", "xingcheng_infer"
    ) == "star-chat"
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        authorize_ai_route(
            "governance/tool/star-chat",
            "xingcheng",
            "xingcheng_train_with_gpt",
        )


def test_task_envelope_rejects_investment_manager_and_sanitizes_memory() -> None:
    task = build_ai_task_envelope(
        provider="gemini",
        business_scope="investment",
        task_type="search",
        content="搜尋官方資料",
        requested_by="xingcheng",
        memory_context=[
            {
                "memory_id": "m1",
                "kind": "source",
                "title": "既有手動設定",
                "content": "未有可靠更新時保留手動配息頻率",
                "origin_model_id": "star-investment-native-model",
                "business_scope": "investment",
                "private_field": "must-not-cross-channel",
            }
        ],
    )

    assert task["schema_version"] == "1.0"
    assert task["response_recipient"] == "xingcheng"
    assert task["memory_policy"]["direct_database_access"] is False
    assert "private_field" not in task["memory_context"][0]
    with pytest.raises(PermissionError, match="PERMISSION_DENIED"):
        build_ai_task_envelope(
            provider="gemini",
            business_scope="investment",
            task_type="search",
            content="越權搜尋",
            requested_by="ai-assistant",
        )


def test_all_external_ai_use_same_provider_browser_without_fallback(
    tmp_path: Path,
) -> None:
    session = AiCollaborationProviderSession(tmp_path)

    class FakeBrowser:
        async def send_prompt(
            self, agent: dict[str, object], _prompt: str
        ) -> dict[str, object]:
            return {
                "status": "completed",
                "provider": agent["provider"],
                "content": "browser response",
                "transport": "embedded-browser-view",
                "uses_api_key": False,
                "fallback": {
                    "used": False,
                    "browser_only": True,
                    "cross_provider_substitution": False,
                },
                "memory_candidates": [],
            }

    session.browser = FakeBrowser()  # type: ignore[assignment]
    task = build_ai_task_envelope(
        provider="gemini",
        business_scope="investment",
        task_type="advanced_search",
        content="尋找官方配息資料",
        requested_by="xingcheng",
    )

    result = asyncio.run(
        session.send_task({"provider": "gemini", "agent_id": "gemini"}, task)
    )

    assert result["status"] == "completed"
    assert result["provider"] == "gemini"
    assert result["transport"] == "embedded-browser-view"
    assert result["fallback"]["used"] is False
    assert result["fallback"]["cross_provider_substitution"] is False
    assert result["memory_candidates"][0]["direct_database_write"] is False


def test_provider_status_declares_all_six_as_browser_automated(tmp_path: Path) -> None:
    session = AiCollaborationProviderSession(tmp_path)
    status = session.provider_status()

    assert {item["provider"] for item in status} == {
        "chatgpt",
        "claude",
        "gemini",
        "grok",
        "deepseek",
        "perplexity",
    }
    assert all(item["browser_only"] is True for item in status)
    assert all(item["automation"] is True for item in status)
    assert all(item["terminal_fallback"] is False for item in status)
