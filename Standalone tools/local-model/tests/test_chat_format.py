# -*- coding: utf-8 -*-
"""star-chat-format/v1：多輪對話渲染、逐輪 label 遮罩、tool-call 切分。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "src"
        / "backend"
        / "services"
        / "xingcheng"
        / "infrastructure"
    ),
)

from native_transformer.chat_format import (  # noqa: E402
    CHAT_FORMAT_VERSION,
    END_OF_TURN,
    ROLE_MARKERS,
    ChatMessage,
    encode_conversation,
    messages_from_pairs,
    render_conversation,
    render_turn,
    split_tool_call,
)
from native_transformer.tokenizer import XingChengTokenizer  # noqa: E402


@pytest.fixture()
def tok():
    return XingChengTokenizer(vocab_size=300)


def test_render_single_turn():
    text = render_turn("user", "你好")
    assert text.startswith(ROLE_MARKERS["user"])
    assert "你好" in text
    assert text.rstrip().endswith(END_OF_TURN)


def test_render_conversation_generation_prompt():
    text = render_conversation(
        [ChatMessage("system", "你是星澄"), ChatMessage("user", "你好")],
        add_generation_prompt=True,
    )
    assert text.endswith(f"{ROLE_MARKERS['assistant']}\n")
    assert ROLE_MARKERS["system"] in text


def test_unknown_role_rejected():
    with pytest.raises(ValueError, match="CHAT_ROLE_UNKNOWN"):
        render_turn("admin", "x")
    with pytest.raises(ValueError, match="CHAT_ROLE_UNKNOWN"):
        ChatMessage(role="root", content="x")


def test_encode_masks_only_assistant_turns(tok):
    messages = [
        {"role": "system", "content": "規則"},
        {"role": "user", "content": "問題一"},
        {"role": "assistant", "content": "回答一"},
        {"role": "user", "content": "問題二"},
        {"role": "assistant", "content": "回答二"},
    ]
    ids, labels = encode_conversation(tok, messages, pad_id=tok.pad_id)
    assert len(ids) == len(labels)
    assert ids[0] == tok.bos_id
    assert labels[0] == tok.pad_id
    trained = [i for i, lab in enumerate(labels) if lab != tok.pad_id]
    trained_text = tok.decode([ids[i] for i in trained])
    assert "回答一" in trained_text and "回答二" in trained_text
    assert "問題一" not in trained_text and "問題二" not in trained_text
    assert "規則" not in trained_text
    # 角色標記本身不參與 loss（只訓練內容與 <|eot|>）
    assert ROLE_MARKERS["assistant"] not in trained_text
    assert ROLE_MARKERS["user"] not in trained_text


def test_encode_truncation_drops_oldest_turns(tok):
    long_content = "很長的對話內容 " * 30
    messages = [
        {"role": "user", "content": long_content},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "Q2"},
        {"role": "assistant", "content": "最新回答"},
    ]
    ids, labels = encode_conversation(
        tok, messages, max_length=64, pad_id=tok.pad_id
    )
    assert len(ids) <= 64
    assert ids[0] == tok.bos_id
    trained_text = tok.decode(
        [ids[i] for i, lab in enumerate(labels) if lab != tok.pad_id]
    )
    assert "最新回答" in trained_text


def test_messages_from_pairs_skips_incomplete():
    convs = messages_from_pairs(
        [
            {"prompt": "Q", "completion": "A"},
            {"prompt": "", "completion": "A"},
            {"input_text": "Q2", "target_text": "A2"},
        ]
    )
    assert len(convs) == 2
    assert convs[0][0].role == "user" and convs[0][1].role == "assistant"


def test_split_tool_call_extracts_and_cleans():
    text = '好的。<tool_call>{"name": "search", "args": {"q": "x"}}</tool_call>完成'
    clean, calls = split_tool_call(text)
    assert calls == [{"name": "search", "args": {"q": "x"}}]
    assert "tool_call" not in clean
    assert "好的" in clean and "完成" in clean


def test_split_tool_call_malformed_fails_closed():
    text = "前<tool_call>{bad json</tool_call>後"
    clean, calls = split_tool_call(text)
    assert calls == []
    assert "<tool_call>" in clean  # 解析失敗保留原文，不產生假呼叫


def test_roundtrip_render_encode(tok):
    messages = [
        ChatMessage("system", "S"),
        ChatMessage("user", "U"),
        ChatMessage("assistant", "A"),
    ]
    rendered = render_conversation(messages)
    assert "<|system|>" in rendered and "<|assistant|>" in rendered
    ids, _ = encode_conversation(tok, messages, pad_id=tok.pad_id)
    decoded = tok.decode(ids[1:], skip_special=False)
    assert len(ids) > 3
    assert decoded  # byte-level tokenizer 可還原文本層標記


def test_format_version():
    assert CHAT_FORMAT_VERSION == "star-chat-format/v1"
