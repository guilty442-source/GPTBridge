"""EC-3 自動記憶端到端：correlation／來源 AI／可查詢重用／免手動卡片。"""
from __future__ import annotations

from _ai_collaboration_test_support import *  # noqa: F401,F403


def test_auto_memory_records_correlation_and_is_queryable(tmp_path: Path) -> None:
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

    memory_item = result["auto_memory_item"]
    assert memory_item is not None, "協作完成須自動寫記憶（免手動卡片）"
    assert memory_item["kind"] == "auto"
    assert memory_item["source_message_id"] == result["group_message"]["message_id"]
    assert memory_item["created_at"], "須記錄時間"
    assert memory_item["content_hash"], "須記錄 content_hash"
    assert "deepseek" in memory_item["source_agent_id"]
    assert "chatgpt" in memory_item["source_agent_id"]
    assert "進行深度推理" in memory_item["content"]
    assert "deepseek" in memory_item["content"]
    assert "已整合" in memory_item["content"]

    # 可查詢重用：list_memory_items 回傳同一筆且 correlation 欄位齊全
    listed = result["memory_items"]
    matched = [m for m in listed if m["memory_id"] == memory_item["memory_id"]]
    assert len(matched) == 1
    assert matched[0]["source_message_id"] == result["group_message"]["message_id"]
    assert matched[0]["status"] == "accepted"


def test_manual_memory_accepts_source_fields(tmp_path: Path) -> None:
    session = _FakeProviderSession()
    service = AiCollaborationService(tmp_path, session=session)

    _event, result = asyncio.run(
        service.handle(
            "ai_nexus_add_memory",
            {
                "kind": "note",
                "title": "手動備註",
                "content": "手動記憶內容",
                "source_agent_id": "chatgpt",
                "source_message_id": "msg-xyz",
            },
        )
    )

    assert result["ok"] is True
    item = result["memory_item"]
    assert item["source_agent_id"] == "chatgpt"
    assert item["source_message_id"] == "msg-xyz"
    assert item["content_hash"]
