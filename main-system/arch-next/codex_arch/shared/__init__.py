"""shared — 法典共享基石。

本層提供三個不可分割的核心機制，讓「決策引用法典（A12）」、
「預設拒絕、明示準予（A10）」與「各主宰單一入口」落實到每一層：

  * contracts — 請求/結果/拒絶的凍結契約型別（fail-closed，A11）。
  * basis     — 決策依據：只允許以法典條文代號引用（A12/E1），
               禁止任何模組內建自然語言決策來源（A38）。
  * gate      — MasterGate：每個主宰唯一公開入口的契約基石；
               未知意圖一律拒絶（A10/A11）。

本層本身無執行權，亦不持有任何可執行元件以外的行為（P2）。
"""

from __future__ import annotations

from .basis import DecisionBasis, codex
from .contracts import (
    Refusal,
    SovereignOutcome,
    SovereignRequest,
)
from .gate import MasterGate, sovereign_entry

__all__ = [
    "DecisionBasis",
    "MasterGate",
    "Refusal",
    "SovereignOutcome",
    "SovereignRequest",
    "codex",
    "sovereign_entry",
]