"""delegation — 受治理執行器委派。

一切實際執行委派受治理執行器，主宰與法典不直接執行（A5/E2）。
本層負責：

  * 登錄受治理執行器（GovernedExecutor）：各自宣告行為邊界（boundary）
    與所需權限意圖，且不得自我準予（A7）。
  * Delegation 契約：經權限主宰明示準予後才可執行；執行前一律以
    permission master entry 重新求值（A10），失敗即關閉（A11）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..shared.contracts import SovereignOutcome, SovereignRequest


class GovernedExecutor(Protocol):
    """受治理執行器契約：宣告邊界，行為唯經委派觸發。"""

    executor_id: str
    boundary: str

    def execute(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ExecutorBinding:
    executor_id: str
    boundary: str
    permission_intent: str
    owner_sovereign: str
    target: str
    implementation: Callable[[dict[str, Any]], dict[str, Any]]


class GovernedExecutorRegistry:
    """登錄受治理執行器；非經登錄之執行器視為不存在（fail-closed）。"""

    def __init__(self) -> None:
        self._bindings: dict[str, ExecutorBinding] = {}

    def register(self, binding: ExecutorBinding) -> None:
        if binding.executor_id in self._bindings:
            raise ValueError(f"duplicate executor {binding.executor_id}")
        self._bindings[binding.executor_id] = binding

    def list(self) -> tuple[str, ...]:
        return tuple(sorted(self._bindings))

    def binding(self, executor_id: str) -> ExecutorBinding | None:
        return self._bindings.get(executor_id)


class Delegation:
    """受治理委派：先經權限主宰求值，通過才交辦受治理執行器。"""

    def __init__(
        self,
        registry: GovernedExecutorRegistry,
        permission_gate: Callable[[SovereignRequest], SovereignOutcome],
        requester: str,
    ) -> None:
        self.registry = registry
        self.permission_gate = permission_gate
        self.requester = requester

    def delegate(
        self,
        executor_id: str,
        payload: dict[str, Any],
    ) -> SovereignOutcome:
        binding = self.registry.binding(executor_id)
        if binding is None:
            return SovereignOutcome(accepted=False, refusal=None, basis=("A10", "A11"))
        permission = self.permission_gate(
            SovereignRequest(
                intent="evaluate",
                subject=binding.permission_intent,
                requester=self.requester,
                payload={key: binding.target for key in ("target",)},
            )
        )
        if not permission.accepted:
            return SovereignOutcome(
                accepted=False,
                refusal=permission.refusal,
                basis=permission.basis,
            )
        try:
            result = binding.implementation(dict(payload))
        except Exception as error:  # noqa: BLE001 - fail-closed boundary
            return SovereignOutcome(
                accepted=False,
                result={"executor": executor_id, "error": error.__class__.__name__},
                basis=("A11",),
            )
        return SovereignOutcome(accepted=True, result=result, basis=("A5", "A10", "E2"))


delegation_registry = GovernedExecutorRegistry()

__all__ = [
    "Delegation",
    "ExecutorBinding",
    "GovernedExecutor",
    "GovernedExecutorRegistry",
    "delegation_registry",
]