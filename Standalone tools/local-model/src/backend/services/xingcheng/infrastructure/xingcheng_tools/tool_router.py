"""Tool Router — 統一工具路由。

對應需求 17, 21：
  Tool Router 至少支援：Local LLM、SQL、RAG、Web Search、Web Fetch、Git、File I/O、Shell
  星澄先判斷需要使用哪一類工具，再執行。
  不得所有問題都啟動全部工具。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Callable

from .intent_router import IntentRouter, IntentResult, SearchIntent


class ToolType(enum.Enum):
    """工具類型。"""

    LOCAL_LLM = "local_llm"
    SQL = "sql"
    RAG = "rag"
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    GIT = "git"
    FILE_IO = "file_io"
    SHELL = "shell"


@dataclass
class ToolRoute:
    """工具路由決策。"""

    primary_tool: ToolType
    secondary_tools: list[ToolType] = field(default_factory=list)
    intent: IntentResult | None = None
    reason: str = ""

    @property
    def all_tools(self) -> list[ToolType]:
        return [self.primary_tool] + self.secondary_tools

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_tool": self.primary_tool.value,
            "secondary_tools": [t.value for t in self.secondary_tools],
            "intent": self.intent.to_dict() if self.intent else None,
            "reason": self.reason,
        }


# 意圖 → 工具映射
_INTENT_TOOL_MAP: dict[SearchIntent, tuple[ToolType, list[ToolType]]] = {
    SearchIntent.NO_SEARCH_NEEDED: (ToolType.LOCAL_LLM, []),
    SearchIntent.LOCAL_DATA_PREFERRED: (ToolType.RAG, [ToolType.SQL, ToolType.LOCAL_LLM]),
    SearchIntent.WEB_SEARCH_RECOMMENDED: (ToolType.RAG, [ToolType.WEB_SEARCH, ToolType.LOCAL_LLM]),
    SearchIntent.WEB_SEARCH_REQUIRED: (ToolType.WEB_SEARCH, [ToolType.WEB_FETCH, ToolType.RAG, ToolType.LOCAL_LLM]),
}


class ToolRouter:
    """統一工具路由器。"""

    def __init__(self, intent_router: IntentRouter | None = None) -> None:
        self.intent_router = intent_router or IntentRouter()
        self._tool_handlers: dict[ToolType, Callable[..., Any]] = {}

    def register_handler(self, tool: ToolType, handler: Callable[..., Any]) -> None:
        self._tool_handlers[tool] = handler

    def get_handler(self, tool: ToolType) -> Callable[..., Any] | None:
        return self._tool_handlers.get(tool)

    def route(self, question: str) -> ToolRoute:
        """根據使用者問題決定工具路由。"""
        intent = self.intent_router.classify(question)
        primary, secondary = _INTENT_TOOL_MAP.get(
            intent.intent, (ToolType.LOCAL_LLM, [])
        )
        return ToolRoute(
            primary_tool=primary,
            secondary_tools=secondary,
            intent=intent,
            reason=f"intent={intent.intent.value} confidence={intent.confidence:.2f}",
        )

    def execute(self, route: ToolRoute, *args: Any, **kwargs: Any) -> Any:
        """執行路由指定的主要工具。"""
        handler = self.get_handler(route.primary_tool)
        if handler is None:
            raise RuntimeError(f"未註冊工具處理器: {route.primary_tool.value}")
        return handler(*args, **kwargs)


__all__ = ["ToolRouter", "ToolRoute", "ToolType"]
