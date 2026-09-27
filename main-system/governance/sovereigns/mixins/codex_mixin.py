"""Sovereign Codex Mixin — codex decision basis and edicts access."""

from __future__ import annotations

from core_system.codex_decision import (
    DecisionBasis,
    codex_edicts,
    codex_edicts_async,
    decision_basis,
    decision_basis_async,
    verified_basis,
    verified_basis_async,
)


class CodexBase:
    """Mixin providing codex decision basis access."""


    def edicts(self) -> list[dict[str, str]]:
        """取得管辖领域的法典敕令（决策依据）。"""
        return codex_edicts(self.area)

    async def edicts_async(self) -> list[dict[str, str]]:
        """事件迴圈安全的 edicts（法典 session 移到工作執行緒）。"""
        return await codex_edicts_async(self.area)

    def basis(self) -> dict[str, Any]:
        """取得完整决策基础（法典+主宰子法）。"""
        return decision_basis(self.area)

    async def basis_async(self) -> dict[str, Any]:
        """事件迴圈安全的 basis（法典 session 移到工作執行緒）。"""
        return await decision_basis_async(self.area)

    def verified_basis(self, *refs: str) -> DecisionBasis:
        """验证并返回决策依据 token。"""
        return verified_basis(refs)

    async def verified_basis_async(self, *refs: str) -> DecisionBasis:
        """事件迴圈安全的 verified_basis（法典 session 移到工作執行緒）。"""
        return await verified_basis_async(refs)

    def _verify_intent(self, intent: str) -> bool:
        """验证意图是否在管辖敕令范围内。"""
        allowed = {e["id"] for e in self.edicts()}
        return intent in allowed or intent.startswith("governance.")


__all__ = ["CodexBase"]