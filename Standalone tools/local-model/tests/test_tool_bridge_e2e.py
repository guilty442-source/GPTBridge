# -*- coding: utf-8 -*-
"""G3 端到端：工具呼叫 → 受治理路由（LocalAiService.handle）→ 執行 → 回執。

與 stub 層測試（test_infer_tool_loop.py）互補：本檔把
``GovernedToolExecutor`` 接到真實 ``LocalAiService``，驗證工具命令真的
走命令 registry → 參數驗證 → handler 這條受管鏈，而非只在模擬層成立。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import _xingcheng_test_support  # noqa: F401

from xingcheng.application.native_tool_orchestrator import GovernedToolExecutor
from xingcheng.application.service import LocalAiService
from xingcheng.infrastructure.native_transformer.inference.chat_session import (
    SessionReply,
)


class _StubSession:
    """最小 ChatSession 介面：回傳預設回覆、記錄回填的工具結果。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.tool_results: list[tuple[str, str]] = []

    def step(self, user_content=None, **_kwargs):
        return self.replies.pop(0)

    def add_tool_result(self, content, *, name=None):
        self.tool_results.append((name, content))


def _reply(text, calls=()):
    return SessionReply(
        text=text,
        raw_text=text,
        tool_calls=tuple(calls),
        generated_tokens=1,
        stopped_by_eos=True,
    )


@pytest.mark.asyncio
async def test_e2e_three_tools_through_real_service(tmp_path: Path) -> None:
    """≥3 個白名單工具經真實 service.handle 執行並回傳結構化結果。"""
    service = LocalAiService(tmp_path)
    executor = GovernedToolExecutor(service)

    for name, arguments in (
        ("status", {}),
        ("memory_list", {"include_inactive": True, "limit": 5}),
        ("sql_list_knowledge", {"limit": 5}),
    ):
        outcome = await executor.execute({"name": name, "arguments": arguments})
        assert outcome["ok"] is True, f"{name}: {outcome}"
        assert outcome["command"].startswith("xingcheng_")
        assert outcome["event"]
        # result 是 handler 回傳的結構化 dict；result_text 超過上限會被截斷
        assert outcome["result"].get("ok") is True


@pytest.mark.asyncio
async def test_e2e_failure_recovers_without_raising(tmp_path: Path) -> None:
    """真實受管鏈上的 handler 失敗（tmp root 非 git repo）必須以
    結構化錯誤回傳，executor 不拋例外、不中斷後續呼叫。"""
    service = LocalAiService(tmp_path)
    executor = GovernedToolExecutor(service)

    outcome = await executor.execute({"name": "platform_status", "arguments": {}})
    assert outcome["ok"] is False
    assert outcome["command"] == "xingcheng_platform_status"
    assert outcome["error_code"] == "TOOL_EXECUTION_FAILED"
    assert "GIT_LOCAL_COMMAND_FAILED" in outcome["result_text"]

    # 失敗後同一 executor 仍可服務下一個工具（迴圈不中斷）。
    follow_up = await executor.execute({"name": "status", "arguments": {}})
    assert follow_up["ok"] is True


@pytest.mark.asyncio
async def test_e2e_converse_round_trip_with_real_dispatch(tmp_path: Path) -> None:
    """session.step 產生工具呼叫 → 真實執行 → tool 訊息回填 → 最終回覆。"""
    service = LocalAiService(tmp_path)
    session = _StubSession(
        [
            _reply("", [{"name": "status", "arguments": {}}]),
            _reply("狀態已回報"),
        ]
    )
    executor = GovernedToolExecutor(service)
    result = await executor.converse(session, "系統狀態如何")
    assert result["ok"] is True
    assert result["text"] == "狀態已回報"
    assert result["tool_rounds"] == 1
    trace = result["tool_trace"]
    assert trace[0]["ok"] is True and trace[0]["command"] == "xingcheng_status"
    name, content = session.tool_results[0]
    assert name == "status" and '"ok": true' in content
