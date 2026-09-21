"""星澄對話格式層（``star-chat-format/v1``）。

角色標記以**文本層**實作：``<|system|>`` / ``<|user|>`` / ``<|assistant|>`` /
``<|tool|>`` / ``<|eot|>`` 由 BPE tokenizer 以普通 token 序列編碼，因此：

- 不更動既有 8,192 詞表，v0 / v1 已訓練權重保持相容；
- 模型仍以語言建模方式學會角色邊界（SFT 語料帶入標記即可）。

將來若要升級為硬 token-ID（真正特殊 token、embedding 擴容），
屬於 tokenizer v2 工作——需重新訓練或 resize embedding，並以
``tokenizer_manifest`` 版本管理；本模組的文本契約維持不變。

職責：

- ``render_conversation``：多輪訊息 → 正規化訓練／推論文本；
- ``encode_conversation``：產生 ``(input_ids, labels)``——只有
  ``assistant`` 角色的內容與結束標記參與 loss，其餘（system / user /
  tool / 角色標記本身 / padding / BOS）全部遮罩；
- ``split_tool_call``：把 assistant 輸出中的 ``star-tool-call/v1``
  標記切出，供工具路由層消費（模型只產生標記，執行權在執行器）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

CHAT_FORMAT_VERSION = "star-chat-format/v1"

ROLE_MARKERS: dict[str, str] = {
    "system": "<|system|>",
    "user": "<|user|>",
    "assistant": "<|assistant|>",
    "tool": "<|tool|>",
}
END_OF_TURN = "<|eot|>"
TOOL_CALL_OPEN = "<tool_call>"
TOOL_CALL_CLOSE = "</tool_call>"

_ALLOWED_ROLES = frozenset(ROLE_MARKERS)


@dataclass(frozen=True)
class ChatMessage:
    """單則對話訊息。"""

    role: str
    content: str

    def __post_init__(self) -> None:
        role = str(self.role).strip().lower()
        if role not in _ALLOWED_ROLES:
            raise ValueError(f"CHAT_ROLE_UNKNOWN:{role}")
        object.__setattr__(self, "role", role)


def _as_message(item: ChatMessage | Mapping[str, Any]) -> ChatMessage:
    if isinstance(item, ChatMessage):
        return item
    return ChatMessage(role=str(item.get("role") or ""), content=str(item.get("content") or ""))


def render_turn(role: str, content: str) -> str:
    """單輪渲染：``<|role|>內容<|eot|>``。"""
    marker = ROLE_MARKERS.get(str(role).strip().lower())
    if marker is None:
        raise ValueError(f"CHAT_ROLE_UNKNOWN:{role}")
    return f"{marker}\n{str(content).strip()}\n{END_OF_TURN}\n"


def render_conversation(
    messages: Sequence[ChatMessage | Mapping[str, Any]],
    *,
    add_generation_prompt: bool = False,
) -> str:
    """多輪對話 → 正規文本。``add_generation_prompt`` 於末端留 assistant 開頭，
    供推論時接續生成。"""
    parts = [render_turn(m.role, m.content) for m in (_as_message(x) for x in messages)]
    if add_generation_prompt:
        parts.append(f"{ROLE_MARKERS['assistant']}\n")
    return "".join(parts)


def _encode_piece(tokenizer, text: str) -> list[int]:
    return list(tokenizer.encode(text, add_bos=False, add_eos=False))


def encode_conversation(
    tokenizer,
    messages: Sequence[ChatMessage | Mapping[str, Any]],
    *,
    max_length: int | None = None,
    pad_id: int | None = None,
) -> tuple[list[int], list[int]]:
    """多輪對話 → ``(input_ids, labels)``。

    label 規則：只有 assistant 輪次的「內容＋結束標記」保留 label；
    assistant 角色標記、system / user / tool 輪、BOS 全以 ``pad_id`` 遮罩。
    截斷時從序列頭部丟棄最早輪次（保留最新上下文），並保證 BOS 保留。
    """
    pad = int(pad_id if pad_id is not None else getattr(tokenizer, "pad_id", 0))
    bos = int(getattr(tokenizer, "bos_id", 1))
    segments: list[tuple[list[int], bool]] = []  # (ids, trainable)
    for raw in messages:
        message = _as_message(raw)
        head = _encode_piece(tokenizer, f"{ROLE_MARKERS[message.role]}\n")
        body = _encode_piece(
            tokenizer, f"{message.content.strip()}\n{END_OF_TURN}\n"
        )
        trainable = message.role == "assistant"
        segments.append((head, False))
        segments.append((body, trainable))

    input_ids = [bos]
    labels = [pad]
    for ids, trainable in segments:
        input_ids.extend(ids)
        labels.extend(ids if trainable else [pad] * len(ids))

    if max_length is not None:
        # 保留 BOS；以輪次（head+body 一對 segment）粒度丟棄最舊上下文。
        while len(input_ids) > int(max_length) and segments:
            drop = len(segments.pop(0)[0])
            if segments:
                drop += len(segments.pop(0)[0])
            del input_ids[1 : 1 + drop]
            del labels[1 : 1 + drop]
        input_ids = input_ids[: int(max_length)]
        labels = labels[: int(max_length)]
    return input_ids, labels


def split_tool_call(text: str) -> tuple[str, list[dict[str, Any]]]:
    """把 assistant 文本中的 ``star-tool-call/v1`` 標記切出。

    回傳 ``(clean_text, calls)``；``calls`` 為已解析 JSON 物件，
    解析失敗的標記保留於原文本（fail-closed，不產生假工具呼叫）。
    """
    calls: list[dict[str, Any]] = []
    remaining = str(text)
    while True:
        start = remaining.find(TOOL_CALL_OPEN)
        if start < 0:
            break
        end = remaining.find(TOOL_CALL_CLOSE, start + len(TOOL_CALL_OPEN))
        if end < 0:
            break
        raw = remaining[start + len(TOOL_CALL_OPEN) : end].strip()
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("tool call payload must be an object")
            calls.append(payload)
            remaining = remaining[:start] + remaining[end + len(TOOL_CALL_CLOSE) :]
        except (ValueError, json.JSONDecodeError):
            break
    return remaining.strip(), calls


def messages_from_pairs(records: Iterable[Mapping[str, Any]]) -> list[list[ChatMessage]]:
    """把 ``prompt``/``completion`` 範例列轉成 user→assistant 雙輪訊息序列。"""
    conversations: list[list[ChatMessage]] = []
    for record in records:
        prompt = str(record.get("prompt") or record.get("input_text") or "").strip()
        completion = str(
            record.get("completion") or record.get("response") or record.get("target_text") or ""
        ).strip()
        if not prompt or not completion:
            continue
        conversations.append(
            [ChatMessage("user", prompt), ChatMessage("assistant", completion)]
        )
    return conversations


__all__ = [
    "CHAT_FORMAT_VERSION",
    "END_OF_TURN",
    "ROLE_MARKERS",
    "TOOL_CALL_CLOSE",
    "TOOL_CALL_OPEN",
    "ChatMessage",
    "encode_conversation",
    "messages_from_pairs",
    "render_conversation",
    "render_turn",
    "split_tool_call",
]
