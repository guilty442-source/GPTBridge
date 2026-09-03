"""gate — 主宰單一入口契約。

「閘門一律限制在主宰層，各主宰單一入口」。MasterGate 提供統一骨架：
請求 → 驗證身分/意圖（明示準予清單） → 委派決策 → 統一輸出。
任何未知意圖、未知身分或未列於清單之作法一律拒絶（A10/A11）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .basis import DecisionBasis, verified_basis
from .contracts import (
    Refusal,
    SovereignOutcome,
    SovereignRequest,
    refusal_outcome,
)

_DEFAULT_REFUSAL_BASIS = tuple(DecisionBasis(("A10", "A11")).references)


@dataclass(frozen=True)
class EntryRule:
    """單一入口規則：用意圖代號開門，行為是私有委派函式。"""

    intent: str
    boundary: str
    handler: Callable[[SovereignRequest], SovereignOutcome]
    basis: tuple[str, ...]

    def refusals(self) -> tuple[str, ...]:
        return self.basis + _DEFAULT_REFUSAL_BASIS


class MasterGate:
    """主宰單一入口骨架：預設拒絶，明示準予才轉發決策。"""

    def __init__(
        self,
        sovereign_id: str,
        *,
        required_roles: frozenset[str],
        rules: tuple[str, ...] = (),
    ) -> None:
        self.sovereign_id = sovereign_id
        self.required_roles = required_roles
        self._rules: dict[str, EntryRule] = {}
        for entry in rules:
            self.add_entry(entry)

    def add_entry(self, entry: EntryRule) -> None:
        all_basis = verified_basis(entry.basis)
        self._rules[entry.intent] = EntryRule(
            intent=entry.intent,
            boundary=entry.boundary,
            handler=entry.handler,
            basis=all_basis.references,
        )

    def intents(self) -> tuple[str, ...]:
        return tuple(sorted(self._rules))

    def entry(self, request: SovereignRequest) -> SovereignOutcome:
        if request.intent not in self._rules:
            return refusal_outcome(
                "UNKNOWN_INTENT", _DEFAULT_REFUSAL_BASIS
            )
        rule = self._rules[request.intent]
        if request.requester not in self.required_roles:
            return refusal_outcome("ROLE_NOT_ALLOWED", rule.refusals())
        return rule.handler(request)

    def __call__(self, request: SovereignRequest) -> SovereignOutcome:
        return self.entry(request)


def sovereign_entry(
    sovereign_id: str,
    *,
    required_roles: frozenset[str],
    intent: str,
    boundary: str,
    basis: tuple[str, ...],
):
    """宣告式註冊：建立一個主宰單一入口條目所需的 EntryRule。"""

    def decorate(
        handler: Callable[[SovereignRequest], SovereignOutcome],
    ) -> EntryRule:
        return EntryRule(
            intent=intent,
            boundary=boundary,
            handler=handler,
            basis=basis,
        )

    return decorate


__all__ = [
    "EntryRule",
    "MasterGate",
    "sovereign_entry",
]