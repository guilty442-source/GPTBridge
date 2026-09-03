"""sovereigns — 主宰體系。

「閘門一律限制在主宰層，各主宰單一入口」。全體系唯一公開入口為
SovereignMasterEntry；任何對主宰的呼叫一律經由 router.route()，未知
主宰或非單一入口之存取一律視為不存在並拒絶（A10/A11）。
"""

from __future__ import annotations

from typing import Any, Protocol

from ..shared.contracts import SovereignOutcome, SovereignRequest, refusal_outcome


class Sovereign(Protocol):
    sovereign_id: str
    codification: tuple[str, ...]
    intents: tuple[str, ...]

    def master_entry(self, request: SovereignRequest) -> SovereignOutcome: ...


class SovereignMasterEntry:
    """全主宰唯一出口：sovereign_id → 各主宰單一入口。"""

    def __init__(self) -> None:
        self._sovereigns: dict[str, Sovereign] = {}

    def register(self, sovereign: Sovereign) -> None:
        if sovereign.sovereign_id in self._sovereigns:
            raise ValueError(f"duplicate sovereign {sovereign.sovereign_id}")
        self._sovereigns[sovereign.sovereign_id] = sovereign

    def sovereign(self, sovereign_id: str) -> Sovereign | None:
        return self._sovereigns.get(sovereign_id)

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._sovereigns))

    def route(
        self, sovereign_id: str, request: SovereignRequest
    ) -> SovereignOutcome:
        sovereign = self._sovereigns.get(sovereign_id)
        if sovereign is None:
            return refusal_outcome("UNKNOWN_SOVEREIGN", ("A10", "A11"))
        return sovereign.master_entry(request)


master_entry_router = SovereignMasterEntry()

__all__ = [
    "Sovereign",
    "SovereignMasterEntry",
    "master_entry_router",
]