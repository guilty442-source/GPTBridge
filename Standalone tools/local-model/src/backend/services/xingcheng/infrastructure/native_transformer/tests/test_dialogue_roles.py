"""Phase 5D — 對話角色與遮罩驗收（藍圖 §2.3／G40）。

角色渲染往返、Assistant 區段 Loss Mask 正確性、截斷保 BOS＋最新輪次、
未知角色 fail-closed、tool_call 解析 fail-closed、續接反例結構。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

import pytest

from native_transformer import XingChengTokenizer
from native_transformer.chat_format import (
    END_OF_TURN,
    ROLE_MARKERS,
    ChatMessage,
    encode_conversation,
    messages_from_pairs,
    render_conversation,
    render_turn,
    split_tool_call,
)


@pytest.fixture()
def tok() -> XingChengTokenizer:
    return XingChengTokenizer(vocab_size=264)


# ---------------------------------------------------------------- 角色渲染往返


def test_role_render_roundtrip() -> None:
    for role in ("system", "user", "assistant", "tool"):
        rendered = render_turn(role, "內容")
        assert rendered.startswith(ROLE_MARKERS[role])
        assert rendered.rstrip().endswith(END_OF_TURN)
        assert "內容" in rendered


def test_unknown_role_rejected() -> None:
    with pytest.raises(ValueError, match="CHAT_ROLE_UNKNOWN"):
        render_turn("admin", "x")
    with pytest.raises(ValueError, match="CHAT_ROLE_UNKNOWN"):
        ChatMessage(role="", content="x")


def test_render_conversation_generation_prompt() -> None:
    msgs = [{"role": "user", "content": "你好"}]
    text = render_conversation(msgs, add_generation_prompt=True)
    assert text.rstrip().endswith(f"{ROLE_MARKERS['assistant']}")
    text2 = render_conversation(msgs)
    assert not text2.rstrip().endswith(ROLE_MARKERS["assistant"])


# ---------------------------------------------------------------- Loss Mask


def test_assistant_body_only_trainable(tok: XingChengTokenizer) -> None:
    pad = int(tok.pad_id)
    msgs = [
        {"role": "system", "content": "你是助理"},
        {"role": "user", "content": "問題一"},
        {"role": "assistant", "content": "回答一"},
        {"role": "user", "content": "問題二"},
        {"role": "assistant", "content": "回答二"},
    ]
    input_ids, labels = encode_conversation(tok, msgs, pad_id=pad)
    assert len(input_ids) == len(labels)
    assert labels[0] == pad, "BOS 必須遮罩"

    # 還原每個可訓練 token 的解碼文字——應全部屬於 assistant 內容＋eot
    trained = [t for t, lb in zip(input_ids, labels) if lb != pad]
    trained_text = tok.decode(trained, skip_special=False)
    assert "回答一" in trained_text and "回答二" in trained_text
    assert "問題一" not in trained_text and "問題二" not in trained_text
    assert "你是助理" not in trained_text
    assert END_OF_TURN in trained_text or "<|eot|>" in trained_text, (
        "assistant 結束標記必須計入 loss（學會收尾）"
    )

    # 逐段重建遮罩：非 assistant 輪次的 head+body 全遮罩，assistant body 全可訓練
    expected_mask: list[bool] = [False]  # BOS
    for m in msgs:
        head = tok.encode(f"{ROLE_MARKERS[m['role']]}\n", add_bos=False, add_eos=False)
        body = tok.encode(
            f"{m['content'].strip()}\n{END_OF_TURN}\n", add_bos=False, add_eos=False
        )
        expected_mask += [False] * len(head)
        expected_mask += [m["role"] == "assistant"] * len(body)
    actual_trainable = [lb != pad for lb in labels]
    assert actual_trainable == expected_mask


def test_mask_position_exact(tok: XingChengTokenizer) -> None:
    """遮罩邊界：assistant 角色標記本身被遮罩，內容第一個 token 起可訓練。"""
    pad = int(tok.pad_id)
    msgs = [{"role": "assistant", "content": "OK"}]
    input_ids, labels = encode_conversation(tok, msgs, pad_id=pad)
    head = tok.encode(f"{ROLE_MARKERS['assistant']}\n", add_bos=False, add_eos=False)
    # head 段全遮罩
    for i in range(1, 1 + len(head)):
        assert labels[i] == pad, f"assistant head 位置 {i} 應遮罩"
    # body 段全可訓練
    body_start = 1 + len(head)
    assert all(lb != pad for lb in labels[body_start:])


def test_truncation_keeps_bos_and_newest(tok: XingChengTokenizer) -> None:
    pad = int(tok.pad_id)
    bos = int(tok.bos_id)
    msgs = [
        {"role": "user", "content": f"第{i}輪{'很長的內容' * 8}"}
        for i in range(6)
    ]
    msgs.append({"role": "assistant", "content": "最新回答"})
    input_ids, labels = encode_conversation(
        tok, msgs, max_length=64, pad_id=pad
    )
    assert input_ids[0] == bos, "截斷後 BOS 必須保留"
    assert len(input_ids) <= 64
    text = tok.decode(input_ids, skip_special=False)
    assert "最新回答" in text, "截斷須保留最新輪次"
    assert "第0輪" not in text, "最舊輪次應被丟棄"


def test_tool_role_masked_but_present(tok: XingChengTokenizer) -> None:
    """工具結果引用：tool 輪內容存在於 input 但遮罩（模型可讀、不學產生）。"""
    pad = int(tok.pad_id)
    msgs = [
        {"role": "user", "content": "查時間"},
        {"role": "tool", "content": '{"time": "12:00"}'},
        {"role": "assistant", "content": "現在十二點"},
    ]
    input_ids, labels = encode_conversation(tok, msgs, pad_id=pad)
    text = tok.decode(input_ids, skip_special=False)
    assert "12:00" in text, "tool 結果必須在輸入中可見"
    tool_content_ids = tok.encode('{"time": "12:00"}', add_bos=False, add_eos=False)
    first = tool_content_ids[0]
    pos = input_ids.index(first)
    assert labels[pos] == pad, "tool 輸出不得計入 loss"


# ---------------------------------------------------------------- tool_call


def test_tool_call_split_parses_valid() -> None:
    text = '好的<tool_call>{"name": "get_time", "arguments": {}}</tool_call>完成'
    clean, calls = split_tool_call(text)
    assert len(calls) == 1 and calls[0]["name"] == "get_time"
    assert "tool_call" not in clean and "好的" in clean and "完成" in clean


def test_tool_call_split_fail_closed_on_bad_json() -> None:
    text = "前<tool_call>{invalid json}</tool_call>後"
    clean, calls = split_tool_call(text)
    assert calls == [], "解析失敗不得產生假呼叫"
    assert "<tool_call>" in clean, "失敗標記保留於原文"


def test_tool_call_unclosed_marker_preserved() -> None:
    _, calls = split_tool_call("文字<tool_call>{\"name\": \"x\"}")
    assert calls == []


# ---------------------------------------------------------------- 續接反例結構


def test_continuation_counterexample_structure() -> None:
    """「繼續」依狀態延續：相同指令在不同上下文的渲染必須不同。"""
    a = render_conversation([
        {"role": "user", "content": "寫到一半"},
        {"role": "assistant", "content": "第一段。"},
        {"role": "user", "content": "繼續"},
    ], add_generation_prompt=True)
    b = render_conversation([
        {"role": "user", "content": "繼續"},
    ], add_generation_prompt=True)
    assert a != b and "第一段。" in a and "第一段。" not in b
    # messages_from_pairs 維持順序
    convs = messages_from_pairs([
        {"prompt": "p1", "completion": "c1"},
        {"prompt": "p2", "completion": "c2"},
    ])
    assert len(convs) == 2
    assert convs[0][0].role == "user" and convs[0][1].role == "assistant"
