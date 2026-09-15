"""Sovereign Codex Mixin — codex decision basis and edicts access."""

from __future__ import annotations

from core_system.codex_decision import (
    DecisionBasis,
    codex_edicts,
    decision_basis,
    verified_basis,
)


class CodexMixin:
    """Mixin providing codex decision basis access."""

    @property
    def area(self) -> str:
        raise NotImplementedError("Subclass must implement 'area' property")

    def edicts(self) -> list[dict[str, str]]:
        """取得管辖领域的法典敕令（决策依据）。"""
        return codex_edicts(self.area)

    def basis(self) -> dict[str, Any]:
        """取得完整决策基础（法典+主宰子法）。"""
        return decision_basis(self.area)

    def verified_basis(self, *refs: str) -> DecisionBasis:
        """验证并返回决策依据 token。"""
        return verified_basis(refs)

    def _verify_intent(self, intent: str) -> bool:
        """验证意图是否在管辖敕令范围内。"""
        allowed = {e["id"] for e in self.edicts()}
        return intent in allowed or intent.startswith("governance.")


__all__ = ["CodexMixin"]