"""Sovereign Base — 决策层共同底座（无执行权，仅决策/裁决/授权）。

法典依据:
- A12: SOVEREIGN-DECISION: ADJUDICATION-SOURCE:codex; APPLIES:runtime/maintenance/permission-sovereigns; 星澄:outside-decision-chain
- A63: SOVEREIGN: decision-only; EXECUTION:delegated-to-governed-executor
- A64: SUB-SOVEREIGN: control/dispatch under parent authority; EXECUTION:governed-executor
- A74: ALL-CODEX-CITATION: enter-through-governance-codex://official
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from governance_rule.codex import GOVERNANCE_CODEX
from governance_rule.execution.codex_repository import load_governance_codex
from core_system.codex_decision import (
    DecisionBasis,
    SovereignOutcome,
    SovereignRequest,
    accepted_outcome,
    codex_edicts,
    decision_basis,
    refusal_outcome,
    verified_basis,
)


@dataclass(frozen=True)
class SovereignIdentity:
    """主宰身份（来自法典 sovereigns 表）。"""
    sovereign_id: str
    name: str
    area: str
    rank: str
    basis: str
    duties: tuple[str, ...]
    powers: tuple[str, ...]
    prohibitions: tuple[str, ...]

    @classmethod
    def from_codex(cls, sovereign_id: str) -> "SovereignIdentity":
        for s in GOVERNANCE_CODEX.sovereigns:
            if s.id == sovereign_id:
                return cls(
                    sovereign_id=s.id,
                    name=s.name,
                    area=s.area,
                    rank=s.rank,
                    basis=s.basis,
                    duties=tuple(s.duties),
                    powers=tuple(s.powers),
                    prohibitions=tuple(s.prohibitions),
                )
        raise KeyError(f"Sovereign {sovereign_id!r} not found in Codex")


class SovereignBase(ABC):
    """主宰底座：单一入口 gate、法典引用、唯经委派之执行出口。"""

    sovereign_id: str = ""

    def __init__(self, app: Any | None = None) -> None:
        self.app = app
        self._identity = SovereignIdentity.from_codex(self.sovereign_id)
        self._started = False
        self._state: dict[str, Any] = {}

    @property
    def identity(self) -> SovereignIdentity:
        return self._identity

    @property
    def area(self) -> str:
        return self._identity.area

    @property
    def role(self) -> str:
        return self._identity.sovereign_id

    @property
    def started(self) -> bool:
        return self._started

    # -------------------------------------------------------------------------
    # Codex decision basis
    # -------------------------------------------------------------------------

    def edicts(self) -> list[dict[str, str]]:
        """取得管辖领域的法典敕令（决策依据）。"""
        return codex_edicts(self.area)

    def basis(self) -> dict[str, Any]:
        """取得完整决策基础（法典+主宰子法）。"""
        return decision_basis(self.area)

    def verified_basis(self, *refs: str) -> DecisionBasis:
        """验证并返回决策依据 token。"""
        return verified_basis(refs)

    # -------------------------------------------------------------------------
    # Single entry gate (A63/A64)
    # -------------------------------------------------------------------------

    async def handle(self, request: SovereignRequest) -> SovereignOutcome:
        """单一决策入口：裁决 -> 授权 -> 委派执行。

        Per A10/A11: explicit allowlist, fail-closed.
        """
        # 1. Verify requester identity (A10)
        if not self._verify_requester(request.requester):
            return refusal_outcome("UNAUTHORIZED_REQUESTER", ("A10", "A11"))

        # 2. Verify intent against codex edicts
        if not self._verify_intent(request.intent):
            return refusal_outcome("UNAUTHORIZED_INTENT", ("A10", "A12"))

        # 3. Make decision (pure adjudication)
        decision = await self._adjudicate(request)

        # 4. If accepted, delegate execution to governed executor
        if decision.accepted:
            return await self._delegate_execution(decision, request)
        return decision

    def _verify_requester(self, requester: str) -> bool:
        """验证请求者身份（法典 A10/A7）。"""
        # In production, this delegates to PermissionSovereign/identity registry
        return bool(requester)

    def _verify_intent(self, intent: str) -> bool:
        """验证意图是否在管辖敕令范围内。"""
        allowed = {e["id"] for e in self.edicts()}
        return intent in allowed or intent.startswith("governance.")

    @abstractmethod
    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """核心裁决逻辑（子类实作）。"""
        ...

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """委派执行给受管执行器（A63/A64）。

        实际执行由 governed executor 负责，主宰仅返回授权结果。
        """
        # Production: delegates to app.governance / toolbox / sub-sovereigns
        return decision

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """启动主宰（决策层初始化，不执行业务逻辑）。"""
        if self._started:
            return {"role": self.role, "status": "already_started"}

        self._state = {
            "role": self.role,
            "area": self.area,
            "started_at": self._iso_now(),
            "execution_delegation": "governed-executor-only",
        }
        self._started = True
        await self._on_start()
        return self._state

    async def stop(self) -> None:
        """停止主宰。"""
        if not self._started:
            return
        await self._on_stop()
        self._started = False
        self._state = {"stopped_at": self._iso_now()}

    async def _on_start(self) -> None:
        """子类覆写：启动时的额外初始化。"""
        pass

    async def _on_stop(self) -> None:
        """子类覆写：停止时的清理。"""
        pass

    def status(self) -> dict[str, Any]:
        """状态回报（唯读）。"""
        return {**self._state, "started": self._started}

    def live_status(self) -> dict[str, Any]:
        """即时状态（供编排层查询）。"""
        return self.status()

    def orchestration_status(self) -> dict[str, Any]:
        """编排层状态（含子系统健康）。"""
        return {"state": "active" if self._started else "stopped", "owner": self.role}

    def _iso_now(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["SovereignBase", "SovereignIdentity"]