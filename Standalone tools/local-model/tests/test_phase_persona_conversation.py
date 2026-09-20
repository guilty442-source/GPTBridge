"""對話式人格管理：設定／顯示／清除皆走治理儲存。"""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401
from _test_model_registry_helpers import _make_service

import asyncio

import pytest

from xingcheng.application.persona_conversation import (
    PERSONA_LABEL,
    detect_persona_command,
    render_persona_prompt,
)


@pytest.mark.parametrize(
    "message,expected_action,expected_text",
    (
        ("設定人格：你是星澄，語氣精確務實。", "set", "你是星澄，語氣精確務實。"),
        ("請把人格設為：只說重點。", "set", "只說重點。"),
        ("把人格設定為：務實", "set", "務實"),
        ("人格設為只說重點。", "set", "只說重點。"),
        ("設定人格：\n你是星澄。\n語氣精確。", "set", "你是星澄。\n語氣精確。"),
        ("你的人格是什麼？", "show", ""),
        ("目前人格設定", "show", ""),
        ("清除人格", "reset", ""),
        ("你好嗎？", "", ""),
        ("什麼是治理規則？", "", ""),
    ),
)
def test_detect_persona_command(message, expected_action, expected_text) -> None:
    assert detect_persona_command(message) == (expected_action, expected_text)


def test_render_persona_prompt_marks_user_message() -> None:
    rendered = render_persona_prompt("你是星澄。", "什麼是治理規則？")
    assert rendered.startswith(PERSONA_LABEL)
    assert "使用者最新訊息：什麼是治理規則？" in rendered
    assert render_persona_prompt("", "你好") == "你好"


def test_conversation_can_set_show_and_reset_persona(tmp_path) -> None:
    service = _make_service(tmp_path)

    _, saved = asyncio.run(
        service.handle(
            "xingcheng_infer",
            {"instruction": "設定人格：你是星澄，語氣精確務實，只說重點。"},
        )
    )
    assert saved["ok"] is True
    assert saved["mode"] == "governed-persona-management"
    assert saved["persona_management"]["action"] == "set"
    assert "已更新人格設定" in saved["response"]

    _, shown = asyncio.run(
        service.handle("xingcheng_infer", {"instruction": "你的人格是什麼？"})
    )
    assert shown["persona_management"]["action"] == "show"
    assert "語氣精確務實" in shown["response"]

    _, reset = asyncio.run(
        service.handle("xingcheng_infer", {"instruction": "清除人格"})
    )
    assert reset["persona_management"]["action"] == "reset"
    assert "已清除人格設定" in reset["response"]

    _, after = asyncio.run(
        service.handle("xingcheng_infer", {"instruction": "顯示人格"})
    )
    assert after["response"] == "目前沒有設定人格。"


def test_persona_set_requires_content(tmp_path) -> None:
    service = _make_service(tmp_path)
    _, result = asyncio.run(
        service.handle("xingcheng_infer", {"instruction": "設定人格："})
    )
    assert result["persona_management"]["action"] == "set"
    assert "請在指令後提供人格內容" in result["response"]
