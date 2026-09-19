"""星澄多輪對話 Session（``star-chat-format/v1`` 驅動）。

職責切分（階段 9 邊界）：

- **本模組**：維護訊息歷史、用 ``chat_format`` 渲染 prompt、
  呼叫 ``Generator`` 生成、把 assistant 輸出中的
  ``star-tool-call/v1`` 標記切成待執行清單。
- **不執行工具**：``SessionReply.tool_calls`` 交給呼叫端
  （治理執行器／tool router）實際執行，結果用 ``add_tool_result``
  以 ``tool`` 角色回填，再走下一輪生成。

這保證「模型決定需要什麼工具、執行器負責執行」的分離——
模型核心不直接觸發任何系統操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import torch

from ..chat_format import (
    ChatMessage,
    encode_conversation,
    render_conversation,
    split_tool_call,
)


@dataclass(frozen=True)
class SessionReply:
    """單輪生成結果。"""

    text: str  # 去掉 tool_call 標記後的可顯示文本
    raw_text: str  # 模型原始輸出（含標記）
    tool_calls: tuple[dict[str, Any], ...]  # 待執行工具呼叫（治理層消化）
    generated_tokens: int
    stopped_by_eos: bool


@dataclass
class ChatSession:
    """一條多輪對話的狀態與生成迴圈。"""

    generator: Any  # Generator
    tokenizer: Any
    system_prompt: str | None = None
    max_context: int | None = None
    messages: list[ChatMessage] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.system_prompt and not self.messages:
            self.messages.append(ChatMessage("system", self.system_prompt))

    def _context_limit(self) -> int:
        limit = int(self.generator.config.max_position_embeddings)
        if self.max_context is not None:
            limit = min(limit, int(self.max_context))
        # 留 generation 空間由呼叫端控制；此處只限 prompt 上限
        return limit

    def prompt_ids(self, *, reserve: int = 1) -> torch.Tensor:
        """目前訊息歷史 → 模型輸入（尾端留 assistant 開頭）。"""
        text = render_conversation(self.messages, add_generation_prompt=True)
        ids = list(
            self.tokenizer.encode(
                text, add_bos=True, add_eos=False, max_length=None
            )
        )
        limit = self._context_limit() - max(0, int(reserve))
        if len(ids) > limit:
            ids = [ids[0]] + ids[-(limit - 1) :]  # 保留 BOS＋最新上下文
        return torch.tensor([ids], dtype=torch.long, device=self.generator.device)

    def add_user_message(self, content: str) -> None:
        self.messages.append(ChatMessage("user", content))

    def add_tool_result(self, content: str, *, name: str | None = None) -> None:
        """把工具執行結果以 tool 角色回填（呼叫端執行完後呼叫）。"""
        body = str(content)
        if name:
            body = f"[{name}] {body}"
        self.messages.append(ChatMessage("tool", body))

    def step(
        self,
        user_content: str | None = None,
        *,
        max_new_tokens: int | None = None,
        sampling: Any = None,
    ) -> SessionReply:
        """跑一輪生成：可選先附加 user 訊息，生成、切 tool_call、落歷史。"""
        if user_content is not None:
            self.add_user_message(user_content)
        ids = self.prompt_ids(
            reserve=max_new_tokens or self.generator.config.max_new_tokens
        )
        out = self.generator.generate(
            ids, max_new_tokens=max_new_tokens, sampling=sampling
        )
        raw = self.tokenizer.decode(out[0].tolist(), skip_special=False)
        eos = getattr(self.tokenizer, "eos_id", 2)
        stopped = bool(
            out.numel() and int(out[0, -1].item()) == int(eos)
        )
        clean, calls = split_tool_call(raw)
        self.messages.append(ChatMessage("assistant", clean or raw))
        return SessionReply(
            text=clean,
            raw_text=raw,
            tool_calls=tuple(calls),
            generated_tokens=int(out.numel()),
            stopped_by_eos=stopped,
        )


__all__ = ["ChatSession", "SessionReply"]
