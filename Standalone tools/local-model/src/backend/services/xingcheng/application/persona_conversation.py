"""對話式人格管理：直接在聊天中設定／檢視／清除星澄人格。

人格一律儲存於受治理的 identity 資料庫（版本歷程與稽核），
對話僅是入口；推論時由伺服器端套用，用戶端不需保存人格設定。
"""

from __future__ import annotations

import re
from typing import Any

PERSONA_LABEL = "星澄人格設定："
USER_MESSAGE_LABEL = "使用者最新訊息："

_SET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^(?:請|幫我)?(?:把)?(?:設定|修改|更新|調整)(?:你的|星澄的)?人格(?:設定)?\s*[:：，,]?\s*(.*)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:請|幫我)?(?:把)?(?:你的|星澄的)?人格(?:設定)?(?:改|設)(?:為|成)\s*[:：，,]?\s*(.*)$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:請|幫我)?(?:把)?(?:你的|星澄的)?人格(?:設定)?(?:為|成)\s*[:：，,]?\s*(.*)$",
        re.IGNORECASE,
    ),
    re.compile(r"^(?:請|幫我)?(?:把)?設定人格\s*[:：，,]?\s*(.*)$", re.IGNORECASE),
)

_SET_PREFIXES: tuple[str, ...] = (
    "設定人格", "修改人格", "更新人格", "調整人格",
    "人格設為", "人格設定為", "人格改成", "人格設成", "設定你的人格",
)


def _strip_separators(value: str) -> str:
    return str(value or "").lstrip(":：，,。 \t").strip()

_SHOW_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:顯示|查看|看|讀取)(?:你的|星澄的)?人格(?:設定)?[？?]?$"),
    re.compile(r"^(?:你|星澄)(?:的)?人格(?:設定)?(?:是|為)?(?:什麼|甚麼|啥)(?:呢)?[？?]?$"),
    re.compile(r"^目前(?:的)?人格(?:設定)?(?:是什麼|為何)?[？?]?$"),
)

_RESET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^(?:清除|刪除|重設|重置|清空)(?:你的|星澄的)?人格(?:設定)?[。.]?$"),
)


def detect_persona_command(message: str) -> tuple[str, str]:
    """回傳 (action, persona_text)；action 為 set/show/reset 或空字串。

    只看訊息的第一行與後續內容：設定時第一行是命令，其餘行視為人格內容。
    """
    text = str(message or "").strip()
    if not text:
        return "", ""
    lines = text.splitlines()
    head = lines[0].strip()
    for pattern in _RESET_PATTERNS:
        if pattern.match(head):
            return "reset", ""
    for pattern in _SHOW_PATTERNS:
        if pattern.match(head):
            return "show", ""
    for pattern in _SET_PATTERNS:
        match = pattern.match(head)
        if match is None:
            continue
        captured = _strip_separators(match.group(1))
        remainder = "\n".join(lines[1:]).strip()
        persona = "\n".join(part for part in (captured, remainder) if part).strip()
        return "set", persona
    if head.startswith(_SET_PREFIXES) and len(lines) > 1:
        persona = "\n".join(lines[1:]).strip()
        if persona:
            return "set", persona
    return "", ""


def render_persona_prompt(persona_text: str, prompt: str) -> str:
    """把人格以固定標記前置，供推論使用（範圍閘門只讀標記後的使用者訊息）。"""
    persona = str(persona_text or "").strip()
    if not persona:
        return prompt
    return f"{PERSONA_LABEL}\n{persona}\n\n{USER_MESSAGE_LABEL}{prompt}"


def persona_management_result(
    action: str,
    *,
    response: str,
    version: Any = None,
    model_id: str = "star-native-language-model",
) -> dict[str, Any]:
    return {
        "ok": True,
        "model": model_id,
        "mode": "governed-persona-management",
        "pipeline": ["persona-command-detection", "identity-governed-write"],
        "token_count": 0,
        "intent": "personality",
        "response": response,
        "generation": {
            "text": response,
            "model_type": "persona-management",
            "token_count": 0,
            "facts_preserved": True,
            "grounding_fallback_used": False,
        },
        "persona_management": {
            "action": action,
            "version": version,
            "governed": True,
            "audit": "identity-save-personality",
        },
        "external_model_used": False,
        "third_party_weights_used": False,
        "star_native_model_used": False,
        "network_used_for_inference": False,
        "facts_locked": True,
    }


__all__ = [
    "PERSONA_LABEL",
    "USER_MESSAGE_LABEL",
    "detect_persona_command",
    "persona_management_result",
    "render_persona_prompt",
]
