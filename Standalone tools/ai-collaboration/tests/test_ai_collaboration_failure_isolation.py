"""EC-7 coverage: failure isolation between agents and EC-8 resource /
privacy declarations (shared browser context, no fallback browser,
tool-database-only, no direct ai-assistant access, no offline queue)."""
from __future__ import annotations

import json

from _ai_collaboration_test_support import *  # noqa: F401,F403


class _FlakyGeneralSession:
    """claude raises; every other provider completes normally."""

    async def send_task(
        self, agent: dict[str, object], _task: dict[str, object]
    ) -> dict[str, object]:
        provider = str(agent["provider"])
        if provider == "claude":
            raise RuntimeError("provider exploded")
        return {
            "status": "completed",
            "provider": provider,
            "content": f"{provider} 回覆",
            "transport": "embedded-browser-view",
            "memory_candidates": [],
        }

    async def close_background_context(self) -> None:
        return None


def test_one_agent_failure_is_isolated_and_siblings_complete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = AiCollaborationService(tmp_path, session=_FlakyGeneralSession())

        _event, result = await service.handle(
            "ai_nexus_send_message",
            {
                "content": "一般協作",
                "agent_ids": ["claude", "gemini"],
                "business_task": "general",
                "_authorized_requester_actor": "governance/tool/ai-collaboration",
            },
        )

        responses = {
            item["agent_id"]: item
            for item in result["group_message"]["responses"]
        }
        assert responses["claude"]["status"] == "failed"
        assert "provider exploded" in responses["claude"]["error"]
        assert responses["gemini"]["status"] == "completed"
        assert responses["gemini"]["content"] == "gemini 回覆"
        assert result["workflow_status"] == "attention-required"

        agents = {item["agent_id"]: item for item in result["agents"]}
        assert agents["claude"]["status"] == "failed"
        assert agents["gemini"]["status"] == "completed"

        _event, state = await service.handle("ai_nexus_get_state", {})
        assert state["diagnostics"]["collaboration"]["failed_responses"] == 1
        assert state["diagnostics"]["collaboration"]["completed_responses"] == 1

    asyncio.run(scenario())


def test_fixed_task_owner_failure_does_not_block_chatgpt_coordination(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = AiCollaborationService(tmp_path, session=_FlakyGeneralSession())

        _event, result = await service.handle(
            "ai_nexus_send_message",
            {
                "content": "撰寫長文",
                "business_task": "longform",
                "business_scope": "general",
            },
        )

        responses = {
            item["agent_id"]: item
            for item in result["group_message"]["responses"]
        }
        # longform fixed owner is claude; its failure stays contained and
        # chatgpt final coordination still runs to completion.
        assert responses["claude"]["status"] == "failed"
        assert responses["chatgpt"]["status"] == "completed"
        assert result["final_response"]["agent_id"] == "chatgpt"
        assert result["final_response"]["status"] == "completed"

    asyncio.run(scenario())


def test_manifest_declares_resource_and_privacy_boundaries() -> None:
    manifest = json.loads(
        (TOOL_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    external = manifest["capabilities"]["external-ai"]
    browser = external["browser"]

    assert external["data_scope"] == "tool-database-only"
    assert external["api"] is False
    assert external["queue_when_offline"] is False
    assert external["direct_ai_assistant_access"] is False
    assert external["direct_user_business_entry"] is False
    assert browser["shared_browser_context"] is True
    assert browser["fallback_browser"] is False
    assert manifest["permissions"]["database_scope"] == "tool-database-only"
    assert manifest["permissions"]["deny"] == ["main-program", "other-tools"]
    assert manifest["request_channel"]["direct_instruction"] == "PERMISSION_DENIED"


def test_isolation_policy_covers_tool_budgets() -> None:
    policy = json.loads(
        (
            Path(__file__).resolve().parents[3]
            / "main-system"
            / "config"
            / "tool-isolation-policy.json"
        ).read_text(encoding="utf-8")
    )
    override = policy["per_tool_overrides"].get("ai-collaboration")
    assert override is not None
    assert override["network_policy"] == "loopback-only"
    assert override["filesystem_policy"] == "tool-scoped"
    assert override["memory_limit_mb"] > 0
    assert override["cpu_percent_limit"] > 0
    assert override["restart_on_crash"] is True
    assert override["max_restart_attempts"] >= 1


def test_ai_assistant_actor_is_denied_direct_access(tmp_path: Path) -> None:
    service = AiCollaborationService(tmp_path, session=_FlakyGeneralSession())

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_send_message",
            {
                "content": "直接下指令",
                "agent_ids": ["chatgpt"],
                "business_task": "general",
                "_authorized_requester_actor": "governance/tool/ai-assistant",
            },
        )
    )

    assert result == {"ok": False, "message": "PERMISSION_DENIED"}
