"""Xingcheng native model status mixin (A185 split).

Contains the capability_status and main_model methods extracted from
XingchengNativeModel.
"""
from __future__ import annotations

from typing import Any

from .xingcheng_native_model_constants import (
    _MAIN_MODEL_ID,
    _MAIN_MODEL_RESPONSIBILITIES,
)


class XingchengNativeModelStatusMixin:
    """Capability status and main model definition."""

    def _xingcheng_capabilities(self) -> dict[str, Any]:
        raise NotImplementedError

    def _business_domain(self, domain_id: str) -> dict[str, Any]:
        raise NotImplementedError

    @property
    def main_model(self) -> dict[str, Any]:
        """主模型 — 綜合能力（協調、理解、整合、檢查、裁決）。

        收斂自 manifest 中的：
          coordination_owner / collaboration_owner / generalist_owner /
          understanding_authority / allocation_authority / integration_authority /
          inspection_authority / result_authority / model_failure_adjudicator /
          analysis_final_coordinator / ai-connections.final_coordinator
        """
        xingcheng_caps = self._xingcheng_capabilities()
        automatic_workflow = xingcheng_caps.get("automatic_workflow") or {}
        ai_connections = self._business_domain("ai-connections")
        investment_analysis = self._business_domain("investment-analysis")

        # 從 manifest 讀取實際模型 ID，fallback 到預設值
        model_id = str(
            xingcheng_caps.get("coordination_owner")
            or xingcheng_caps.get("generalist_owner")
            or automatic_workflow.get("understanding_authority")
            or _MAIN_MODEL_ID
        )

        return {
            "mode": "main",
            "model": model_id,
            "role": "comprehensive",
            "responsibilities": list(_MAIN_MODEL_RESPONSIBILITIES),
            "manifest_mapping": {
                "coordination_owner": str(xingcheng_caps.get("coordination_owner") or ""),
                "collaboration_owner": str(xingcheng_caps.get("collaboration_owner") or ""),
                "generalist_owner": str(xingcheng_caps.get("generalist_owner") or ""),
                "understanding_authority": str(automatic_workflow.get("understanding_authority") or ""),
                "allocation_authority": str(automatic_workflow.get("allocation_authority") or ""),
                "integration_authority": str(automatic_workflow.get("integration_authority") or ""),
                "inspection_authority": str(automatic_workflow.get("inspection_authority") or ""),
                "result_authority": str(automatic_workflow.get("result_authority") or ""),
                "model_failure_adjudicator": str(automatic_workflow.get("model_failure_adjudicator") or ""),
                "analysis_final_coordinator": str(investment_analysis.get("analysis_final_coordinator") or ""),
                "ai_connections_final_coordinator": str(ai_connections.get("final_coordinator") or ""),
            },
        }

    def capability_status(self) -> dict[str, Any]:
        """原生模型能力的完整快照（收攏所有能力面 + 三模式收斂）。"""
        return {
            # 1. 核心
            "core": {
                "kind": self.kind,
                "module_id": self.module_id,
                "native_model_authority": self.native_model_authority,
                "model_permissions": self.model_permissions,
                "empowered_powers": list(self.empowered_powers),
                "prohibited_powers": list(self.prohibited_powers),
                "fixed_responsibilities": self.fixed_responsibilities,
                "entry_ownership": self.entry_ownership,
                "entry_sources": self.entry_sources,
                "specialist_direct_access": self.specialist_direct_access,
                "fallback_model": self.fallback_model,
            },
            # 2. 能力分類
            "capabilities": {
                "capability_categories": self.capability_categories,
                "modules": self.modules,
                "semantic_planning": self.semantic_planning,
                "semantic_understanding": self.semantic_understanding,
                "max_concurrency": self.max_concurrency,
            },
            # 3. 模型模式收斂（核心重組）
            "model_modes": self.model_modes,
            # 向後相容
            "model_registry": self.model_registry,
            "autonomous_agent": self.autonomous_agent,
            "automatic_workflow": self.automatic_workflow,
            "dynamic_model_dispatch": self.dynamic_model_dispatch,
            # 4. 業務能力面
            "business_domains": {
                "ids": self.business_domain_ids,
                "investment_market_search": self.investment_market_search,
                "investment_analysis": self.investment_analysis,
                "upgrade_optimization": self.upgrade_optimization,
                "star_project_programming": self.star_project_programming,
                "ai_connections": self.ai_connections,
                "document_reading": self.document_reading,
            },
            # 共同邊界
            "execution": False,
            "delegation": "governed-executor-only",
        }


__all__ = ["XingchengNativeModelStatusMixin"]
