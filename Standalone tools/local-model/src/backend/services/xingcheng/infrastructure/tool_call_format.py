"""``star-tool-call/v1``：工具呼叫 schema 與 SFT 訓練樣本格式。

訓練樣本以確定性標記把工具呼叫嵌入 completion 文本：

    <tool_call>{"name": "<tool>", "arguments": {...}}</tool_call>

產出記錄與 ``sft_dataset.serialize_sft_example`` 相容，可直接進
``build_sft_dataset`` / 受管訓練管線。
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

TOOL_CALL_FORMAT_VERSION = "star-tool-call/v1"

_TOOL_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
_MAX_CALLS = 8
_MAX_ARGUMENTS_BYTES = 65_536
_MAX_TOOLS = 32


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def normalize_tool_call(call: Mapping[str, Any]) -> dict[str, Any]:
    """驗證並正規化單一工具呼叫。"""
    name = str(call.get("name") or "").strip()
    if not _TOOL_NAME.fullmatch(name):
        raise ValueError(f"invalid tool name: {name!r}")
    arguments = call.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        raise ValueError("tool call arguments must be an object")
    encoded = _canonical_json(arguments)
    if len(encoded.encode("utf-8")) > _MAX_ARGUMENTS_BYTES:
        raise ValueError("tool call arguments exceed bounded size")
    return {"name": name, "arguments": dict(arguments)}


def normalize_tool_spec(spec: Mapping[str, Any]) -> dict[str, Any]:
    """驗證工具定義（prompt 內宣告可用工具的 schema 片段）。"""
    name = str(spec.get("name") or "").strip()
    if not _TOOL_NAME.fullmatch(name):
        raise ValueError(f"invalid tool spec name: {name!r}")
    description = str(spec.get("description") or "").strip()
    parameters = spec.get("parameters")
    if parameters is None:
        parameters = {"type": "object", "properties": {}}
    if not isinstance(parameters, dict):
        raise ValueError("tool spec parameters must be an object")
    return {
        "name": name,
        "description": description,
        "parameters": dict(parameters),
    }


def encode_tool_calls(tool_calls: Sequence[Mapping[str, Any]]) -> str:
    """工具呼叫序列 → 確定性標記文本。"""
    if len(tool_calls) > _MAX_CALLS:
        raise ValueError(f"tool calls exceed bounded count {_MAX_CALLS}")
    return "".join(
        f"<tool_call>{_canonical_json(normalize_tool_call(call))}</tool_call>"
        for call in tool_calls
    )


def decode_tool_calls(text: str) -> list[dict[str, Any]]:
    """從生成文本取出所有 ``<tool_call>`` 區塊（無標記回傳空清單）。"""
    calls: list[dict[str, Any]] = []
    for match in re.finditer(
        r"<tool_call>(.*?)</tool_call>", str(text), flags=re.DOTALL
    ):
        try:
            payload = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "name" in payload:
            calls.append(normalize_tool_call(payload))
    return calls


def serialize_tool_call_example(
    *,
    prompt: str,
    tool_calls: Sequence[Mapping[str, Any]],
    response_text: str = "",
    tools: Sequence[Mapping[str, Any]] | None = None,
    intent: str = "tool_call",
    source_example_id: str = "",
    source_type: str = "owner-governed-tool-call",
    quality_score: float = 0.9,
) -> dict[str, Any]:
    """組成一筆 SFT 相容的工具呼叫訓練樣本。"""
    normalized_prompt = str(prompt or "").strip()
    if not normalized_prompt:
        raise ValueError("tool call sample requires a prompt")
    normalized_tools = [
        normalize_tool_spec(spec) for spec in list(tools or [])[:_MAX_TOOLS]
    ]
    call_markup = encode_tool_calls(tool_calls)
    completion = f"{call_markup}{str(response_text or '').strip()}"
    if not completion.strip():
        raise ValueError("tool call sample requires calls or response text")
    text = f"{normalized_prompt}\n\n{completion}"
    return {
        "format_version": TOOL_CALL_FORMAT_VERSION,
        "source": source_type,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": text,
        "prompt": normalized_prompt,
        "completion": completion,
        "intent": str(intent or "tool_call"),
        "source_example_id": str(source_example_id or ""),
        "quality_score": max(0.0, min(1.0, float(quality_score))),
        "tools": normalized_tools,
        "tool_calls": [
            normalize_tool_call(call) for call in tool_calls
        ],
    }


__all__ = [
    "TOOL_CALL_FORMAT_VERSION",
    "decode_tool_calls",
    "encode_tool_calls",
    "normalize_tool_call",
    "normalize_tool_spec",
    "serialize_tool_call_example",
]
