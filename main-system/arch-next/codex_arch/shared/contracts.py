"""contracts — 主宰層契約型別（凍結，無行為）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class SovereignRequest:
    """送至某主宰單一入口的請求。

    intent 與 subject 均為法典式代號（kebab-case token），非自然語言；
    身分 requester 必須是已發放之角色（A7/A10）。
    """

    intent: str
    subject: str
    requester: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Refusal:
    """fail-closed 拒絶記錄（A11）：不揭露內部權限細節。"""

    reason_code: str
    basis: tuple[str, ...] = ()


@dataclass(frozen=True)
class SovereignOutcome:
    """主宰決策之統一輸出。

    accepted 一律為預設 False；只有通過明示準予清單才為 True（A10）。
    """

    accepted: bool = False
    refusal: Refusal | None = None
    result: dict[str, Any] = field(default_factory=dict)
    basis: tuple[str, ...] = ()


def refusal_outcome(reason_code: str, basis: tuple[str, ...]) -> SovereignOutcome:
    return SovereignOutcome(accepted=False, refusal=Refusal(reason_code, basis), basis=basis)


def accepted_outcome(result: dict[str, Any], basis: tuple[str, ...]) -> SovereignOutcome:
    return SovereignOutcome(accepted=True, result=result, basis=basis)


__all__ = [
    "Refusal",
    "SovereignOutcome",
    "SovereignRequest",
    "accepted_outcome",
    "refusal_outcome",
]