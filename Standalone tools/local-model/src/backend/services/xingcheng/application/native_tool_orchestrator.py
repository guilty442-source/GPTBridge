"""星澄工具橋（``star-tool-bridge/v1``）：模型輸出 → 受管命令執行。

階段 9 的執行側：``ChatSession`` 把 assistant 輸出中的
``star-tool-call/v1`` 標記切成待執行清單後，本模組負責：

- 把模型面向的工具名映射到服務命令（唯讀白名單）；
- 參數逐欄位過濾與型別約束（未知參數直接丟棄）；
- 透過 ``service.handle(command, payload)`` 分派——與前端命令走
  同一條受管鏈，不開新的執行面；
- 把結果以 ``tool`` 角色訊息回填 session，驅動下一輪生成。

邊界：白名單僅含唯讀命令（status/rag/git 查詢/記憶列表/診斷），
寫入型命令（save/stage/commit/review）與 ``external_research`` 網路
研究一律不接受模型直接觸發——後者仍是 payload 層的顯式 opt-in。
模型核心本身不執行任何工具，本模組是治理層的執行器。
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Mapping

from ..infrastructure.native_transformer.chat_format import split_tool_call
from ..infrastructure.tool_call_format import (
    TOOL_CALL_FORMAT_VERSION,
    normalize_tool_call,
    normalize_tool_spec,
)

TOOL_BRIDGE_VERSION = "star-tool-bridge/v1"


def _str(value: Any) -> str:
    return str(value)


def _int(value: Any) -> int:
    return int(value)


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes", "on"}
    return bool(value)


#: 模型面向工具名 → 服務命令。``args`` 為允許傳遞的參數與轉型函式；
#: ``static`` 為固定注入的 payload 欄位（模型不可覆寫）。
TOOL_COMMAND_MAP: dict[str, dict[str, Any]] = {
    "status": {
        "command": "xingcheng_status",
        "args": {},
        "description": "星澄整體狀態（模型、資料庫、訓練進度）",
    },
    "platform_status": {
        "command": "xingcheng_platform_status",
        "args": {},
        "description": "平台狀態（git/RAG/LLM/治理來源）",
    },
    "git_status": {
        "command": "xingcheng_git_status",
        "args": {},
        "description": "儲存庫 git 狀態",
    },
    "git_history": {
        "command": "xingcheng_git_history",
        "args": {"limit": _int},
        "description": "近期 git 提交歷史",
    },
    "rag_query": {
        "command": "xingcheng_rag_query",
        "args": {"question": _str, "query": _str, "top_k": _int},
        "static": {"generate": False},
        "description": "本地 RAG 檢索證據（只回 citation，不代答）",
    },
    "knowledge_search": {
        "command": "xingcheng_knowledge_unified_search",
        "args": {"query": _str, "question": _str, "limit": _int},
        "description": "知識庫統一搜尋",
    },
    "memory_list": {
        "command": "xingcheng_memory_list",
        "args": {"include_inactive": _bool, "limit": _int},
        "description": "列出星澄記憶項目",
    },
    "sql_list_knowledge": {
        "command": "xingcheng_sql_list_knowledge",
        "args": {"knowledge_type": _str, "limit": _int},
        "description": "列出本地知識庫條目",
    },
    "diagnose_fault": {
        "command": "xingcheng_diagnose_fault",
        "args": {"symptom": _str, "question": _str},
        "description": "唯讀故障診斷（不含外部網路研究）",
    },
    "search_investments": {
        "command": "xingcheng_search_investments",
        "args": {"query": _str, "limit": _int},
        "description": "投資／市場資料查詢（本地來源）",
    },
}

_MAX_ARG_VALUE_CHARS = 2_000
_MAX_CALLS_PER_ROUND = 8


class GovernedToolExecutor:
    """模型工具呼叫的受管執行器（唯讀白名單＋參數過濾＋稽核追蹤）。"""

    MAX_ROUNDS = 4
    MAX_RESULT_CHARS = 4_000

    def __init__(
        self,
        service: Any,
        *,
        max_rounds: int | None = None,
        max_result_chars: int | None = None,
    ) -> None:
        self._service = service
        self._max_rounds = max(1, int(max_rounds or self.MAX_ROUNDS))
        self._max_result_chars = max(256, int(max_result_chars or self.MAX_RESULT_CHARS))

    @staticmethod
    def available_tool_specs() -> list[dict[str, Any]]:
        """可供塞入 system prompt 的工具宣告（``star-tool-call/v1`` schema）。"""
        specs: list[dict[str, Any]] = []
        for name, entry in TOOL_COMMAND_MAP.items():
            properties = {
                arg: {"type": "string" if fn is _str else ("integer" if fn is _int else "boolean")}
                for arg, fn in entry["args"].items()
            }
            specs.append(
                normalize_tool_spec(
                    {
                        "name": name,
                        "description": entry["description"],
                        "parameters": {"type": "object", "properties": properties},
                    }
                )
            )
        return specs

    def _payload_for(self, entry: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = dict(entry.get("static") or {})
        for arg, coerce in entry["args"].items():
            if arg not in arguments:
                continue
            try:
                value = coerce(arguments[arg])
            except (TypeError, ValueError):
                continue
            if isinstance(value, str):
                value = value[: _MAX_ARG_VALUE_CHARS]
            payload[arg] = value
        return payload

    async def _dispatch(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        outcome = self._service.handle(command, payload)
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if isinstance(outcome, tuple) and len(outcome) == 2:
            event, result = outcome
            return str(event), result if isinstance(result, dict) else {"value": result}
        return "", outcome if isinstance(outcome, dict) else {"value": outcome}

    async def execute(self, call: Mapping[str, Any]) -> dict[str, Any]:
        """執行單一工具呼叫；任何失敗都以結構化錯誤回傳，不中斷迴圈。"""
        try:
            normalized = normalize_tool_call(call)
        except ValueError as error:
            return {
                "ok": False,
                "error_code": "TOOL_CALL_INVALID",
                "message": str(error),
                "result_text": f"[TOOL_CALL_INVALID] {error}",
            }
        name = normalized["name"]
        entry = TOOL_COMMAND_MAP.get(name)
        if entry is None:
            return {
                "ok": False,
                "name": name,
                "error_code": "TOOL_NOT_ALLOWED",
                "message": f"工具 {name} 不在受管白名單",
                "result_text": f"[TOOL_NOT_ALLOWED] {name}",
            }
        command = str(entry["command"])
        payload = self._payload_for(entry, normalized["arguments"])
        try:
            event, result = await self._dispatch(command, payload)
        except Exception as error:  # noqa: BLE001 - 工具失敗不得擊穿對話迴圈
            return {
                "ok": False,
                "name": name,
                "command": command,
                "error_code": "TOOL_EXECUTION_FAILED",
                "message": str(error)[:300],
                "result_text": f"[TOOL_EXECUTION_FAILED] {name}: {str(error)[:200]}",
            }
        result_text = json.dumps(result, ensure_ascii=False, sort_keys=True)
        if len(result_text) > self._max_result_chars:
            result_text = result_text[: self._max_result_chars] + "…"
        return {
            "ok": bool(result.get("ok", True)),
            "name": name,
            "command": command,
            "event": event,
            "result": result,
            "result_text": result_text,
        }

    async def converse(
        self,
        session: Any,
        user_text: str,
        *,
        max_new_tokens: int | None = None,
        sampling: Any = None,
    ) -> dict[str, Any]:
        """完整對話迴圈：生成 → 工具執行 → 回填 → 再生成，直到無呼叫或達上限。"""
        reply = session.step(
            user_text, max_new_tokens=max_new_tokens, sampling=sampling
        )
        trace: list[dict[str, Any]] = []
        rounds = 0
        while reply.tool_calls and rounds < self._max_rounds:
            rounds += 1
            for call in list(reply.tool_calls)[:_MAX_CALLS_PER_ROUND]:
                outcome = await self.execute(call)
                trace.append(
                    {
                        "round": rounds,
                        "name": outcome.get("name") or call.get("name"),
                        "command": outcome.get("command"),
                        "ok": outcome["ok"],
                        "error_code": outcome.get("error_code", ""),
                    }
                )
                session.add_tool_result(
                    outcome["result_text"], name=str(call.get("name") or "")
                )
            reply = session.step(max_new_tokens=max_new_tokens, sampling=sampling)
        return {
            "ok": True,
            "format_version": TOOL_BRIDGE_VERSION,
            "text": reply.text,
            "raw_text": reply.raw_text,
            "tool_calls_pending": list(reply.tool_calls),
            "tool_trace": trace,
            "tool_rounds": rounds,
            "rounds_exhausted": bool(reply.tool_calls),
            "stopped_by_eos": reply.stopped_by_eos,
            "generated_tokens": reply.generated_tokens,
        }

    @staticmethod
    def tool_calls_from_text(text: str) -> list[dict[str, Any]]:
        """非 session 路徑：直接從輸出文本切出工具呼叫。"""
        _clean, calls = split_tool_call(text)
        return calls


__all__ = [
    "TOOL_BRIDGE_VERSION",
    "TOOL_COMMAND_MAP",
    "GovernedToolExecutor",
    "TOOL_CALL_FORMAT_VERSION",
]
